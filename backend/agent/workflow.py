"""Bounded LangGraph orchestration with verification after claim construction."""
from __future__ import annotations

import asyncio
import re
from typing import TypedDict
from uuid import uuid4

from langgraph.graph import END, START, StateGraph

from .evidence import (compare_pair, comparison_claim, make_evidence, record_claim,
                       verify_claims)
from .llm import LanguageModel, ModelNotConfigured
from .models import (Claim, Comparison, ComparisonMetric, Composition, EvidenceRecord,
                     Plan, QueryRequest, QueryResponse)
from .settings import Settings
from .tools import DatabaseTools, ToolCall, ToolResult, tool_log


class AgentState(TypedDict, total=False):
    request: QueryRequest
    trace_id: str
    plan: Plan
    calls: list[ToolCall]
    results: list[ToolResult]
    evidence: list[EvidenceRecord]
    comparisons: list[ComparisonMetric]
    claims: list[Claim]
    unsupported: list[str]
    codes: list[str]
    fatal: bool
    stop: bool
    response: QueryResponse


UNSUPPORTED_PATTERNS = {
    'crime':r'\b(crime|criminal|burglary|theft|robber\w*)\b',
    'safety':r'\b(safe|safer|safest|unsafe|safety|risk score)\b',
    'commute':r'\b(commut\w*|travel time|bus arrival)\b',
    'amenities':r'\b(amenit\w*|grocer\w*|restaurants?)\b',
    'development':r'\b(permits?|construction|development)\b',
    'rental_ranking':r'\b(best apartment|rank apartments|rental price)\b',
    'service_category_breakdown':r'\b(potholes?|streetlights?|graffiti|trash|sidewalks?|service categor\w*)\b',
    'service_status_breakdown':r'\b(closed requests?|open requests?|repaired|repair completion|request status)\b',
}
SUPPORTED_PATTERN=r'\b(collisions?|traffic|crashes?|get it done|service requests?|complaints?|public service|potholes?)\b'


def guard_scope(request: QueryRequest) -> tuple[list[str], bool]:
    """Conservative unsupported routing, independent of model cooperation."""
    text=(request.question+' '+' '.join(request.preferences)).lower().replace('_',' ')
    unsupported=[name for name,pattern in UNSUPPORTED_PATTERNS.items() if re.search(pattern,text)]
    unknown=[p for p in request.preferences if p not in {'traffic_collisions','service_requests'}
             and p not in unsupported]
    if unknown:
        unsupported.append('unavailable_dimension')
    only_unavailable_service=('service_category_breakdown' in unsupported or 'service_status_breakdown' in unsupported)
    stop=bool(unsupported and not re.search(SUPPORTED_PATTERN,text))
    if only_unavailable_service and not re.search(r'\b(collisions?|traffic|crashes?)\b',text):
        stop=True
    return sorted(set(unsupported)),stop


def validated_calls(request: QueryRequest, plan: Plan, settings: Settings) -> list[ToolCall]:
    """Compile a symmetric plan; never accept model-created geographic scope."""
    if len(plan.tools)!=len(set(plan.tools)):
        raise ValueError('Duplicate planned tools')
    if plan.intent=='unsupported_dimension' and plan.tools:
        raise ValueError('Unsupported intent cannot execute data tools')
    if plan.intent=='compare_locations' and len(request.locations)!=2:
        raise ValueError('Comparison requires two pins')
    if plan.intent=='collision_trend' and 'aggregate_traffic_collisions' not in plan.tools:
        raise ValueError('Trends require aggregation')
    if plan.intent=='collision_drilldown' and 'get_traffic_collisions' not in plan.tools:
        raise ValueError('Drilldown requires bounded retrieval')
    if plan.intent=='request_count' and 'count_service_requests' not in plan.tools:
        raise ValueError('Request counts require the service counter')
    if len(request.locations)==2 and plan.tools and 'get_traffic_collisions' in plan.tools:
        # A page is not a comparable count. Drilldown remains possible, but a
        # two-pin collision comparison also needs authoritative aggregation.
        if 'aggregate_traffic_collisions' not in plan.tools:
            raise ValueError('Two-pin collision plans require aggregation, not page sizes')
    unsupported,_=guard_scope(request)
    unavailable_service=any(x in unsupported for x in ('service_category_breakdown','service_status_breakdown'))
    if unavailable_service and 'count_service_requests' in plan.tools:
        raise ValueError('An all-category count cannot substitute for unavailable service filters')
    for pref,tool in [('service_requests','count_service_requests'),('traffic_collisions','aggregate_traffic_collisions')]:
        if pref=='service_requests' and unavailable_service:
            continue
        if pref in request.preferences and tool not in plan.tools:
            raise ValueError('Plan omitted an explicitly requested supported dimension')
    nondefault=plan.exclude_linked_duplicates or any(v is not None for v in plan.collision_filters.model_dump().values())
    if nondefault and (not plan.filter_quote or plan.filter_quote not in request.question):
        raise ValueError('Nondefault filters need an exact supporting question quote')
    if len(plan.tools)*len(request.locations)>settings.max_tool_calls:
        raise ValueError('Tool-call budget exceeded')
    if not plan.tools and not plan.unsupported_dimensions:
        raise ValueError('Empty plan without an unsupported reason')
    return [ToolCall(name=tool,location=loc,radius_meters=request.radius_meters,
        time_range=request.time_range,collision_filters=plan.collision_filters,
        period=plan.period,exclude_linked_duplicates=plan.exclude_linked_duplicates,
        limit=settings.max_rows) for tool in plan.tools for loc in request.locations]


INTRODUCTIONS={
    'records_only':'These findings describe records in the imported datasets.',
    'comparison_only':'This comparison describes imported record counts, not safety or service quality.',
    'trend_only':'Calendar counts describe imported records; changes do not establish changes in underlying risk.',
}


def render_answer(request: QueryRequest, claims: list[Claim], unsupported: list[str],
                  composition: Composition | None = None) -> str:
    """Render only independently verifiable numerical facts and fixed caveats."""
    style=composition.introduction if composition else ('comparison_only' if len(request.locations)==2 else 'records_only')
    parts=[INTRODUCTIONS[style]]
    if claims:
        parts.append(f'Scope: within {request.radius_meters:g} meters of each selected pin, '
            f'from {request.time_range.start.isoformat()} (inclusive) to '
            f'{request.time_range.end.isoformat()} (exclusive); calendar timezone '
            f'{request.time_range.calendar_timezone}.')
        parts.extend(c.text for c in claims)
        parts.append('Coverage and freshness are unknown. Unlocated collision reports are excluded; '
            'collision positions are block-approximate. Zero matches do not establish absence of incidents.')
    elif not unsupported:
        parts.append('No numerical finding could be verified from the available tool results.')
    if unsupported:
        parts.append('The requested unsupported dimensions cannot be assessed with the available data: '+', '.join(unsupported)+'.')
    return ' '.join(parts)


class Agent:
    def __init__(self, model: LanguageModel, tools: DatabaseTools, settings: Settings):
        self.model,self.tools,self.settings=model,tools,settings
        graph=StateGraph(AgentState)
        graph.add_node('resolve_scope',self.resolve_scope)
        graph.add_node('build_plan',self.build_plan)
        graph.add_node('execute_tools',self.execute_tools)
        graph.add_node('normalize_evidence',self.normalize_evidence)
        graph.add_node('construct_claims',self.construct_claims)
        graph.add_node('verify_claims',self.verify)
        graph.add_node('compose_answer',self.compose)
        graph.add_node('final_validate',self.final_validate)
        graph.add_node('abstain',self.abstain)
        graph.add_edge(START,'resolve_scope')
        graph.add_conditional_edges('resolve_scope',lambda s:'abstain' if s.get('stop') else 'build_plan')
        graph.add_conditional_edges('build_plan',lambda s:'abstain' if s.get('stop') else 'execute_tools')
        graph.add_edge('execute_tools','normalize_evidence')
        graph.add_edge('normalize_evidence','construct_claims')
        graph.add_edge('construct_claims','verify_claims')
        graph.add_conditional_edges('verify_claims',lambda s:'abstain' if s.get('fatal') else 'compose_answer')
        graph.add_edge('compose_answer','final_validate')
        graph.add_edge('final_validate',END)
        graph.add_edge('abstain',END)
        self.graph=graph.compile()

    async def query(self, request: QueryRequest) -> QueryResponse:
        state=await self.graph.ainvoke({'request':request,'trace_id':'trace_'+uuid4().hex,
            'codes':[],'unsupported':[],'evidence':[],'claims':[],'comparisons':[]},
            config={'recursion_limit':16})
        return state['response']

    def resolve_scope(self,state: AgentState) -> dict:
        unsupported,stop=guard_scope(state['request'])
        return {'unsupported':unsupported,'stop':stop}

    async def build_plan(self,state: AgentState) -> dict:
        try:
            raw=await asyncio.wait_for(self.model.plan(state['request']),self.settings.llm_timeout_seconds)
            plan=Plan.model_validate(raw.model_dump())
            calls=validated_calls(state['request'],plan,self.settings)
            return {'plan':plan,'calls':calls,'stop':not calls,
                    'unsupported':sorted(set(state['unsupported']+plan.unsupported_dimensions))}
        except ModelNotConfigured:
            return {'stop':True,'fatal':True,'codes':state['codes']+['openai_not_configured']}
        except TimeoutError:
            return {'stop':True,'fatal':True,'codes':state['codes']+['planning_timeout']}
        except Exception:
            return {'stop':True,'fatal':True,'codes':state['codes']+['planning_failed']}

    async def execute_tools(self,state: AgentState) -> dict:
        results=[]
        # Validated duplicate-free plans provide request-local deduplication.
        # No cross-request cache is used without a trustworthy dataset version.
        for call in state['calls']:
            try:
                result=await asyncio.to_thread(self.tools.execute,call)
            except Exception:
                result=ToolResult(call,status='error',error_code='tool_execution_failed')
            results.append(result)
        return {'results':results}

    def normalize_evidence(self,state: AgentState) -> dict:
        evidence=[]
        for result in state['results']:
            try:
                evidence.append(make_evidence(result))
            except Exception:
                failed=ToolResult(result.call,status='error',error_code='invalid_tool_result',
                                  query_id=result.query_id,executed_at=result.executed_at)
                evidence.append(make_evidence(failed))
        return {'evidence':evidence}

    def construct_claims(self,state: AgentState) -> dict:
        evidence=state['evidence']
        claims=[record_claim(e) for e in evidence if e.value is not None]
        comparisons=[]
        request=state['request']
        if len(request.locations)==2:
            for metric in sorted({e.metric for e in evidence if e.metric!='retrieved_collision_page_size'}):
                pair=[next((e for e in evidence if e.metric==metric and e.location_id==loc.id),None)
                      for loc in request.locations]
                if all(e is not None and e.value is not None for e in pair):
                    try:
                        compared=compare_pair(*pair)
                    except ValueError:
                        continue
                    comparisons.append(compared)
                    claims.append(comparison_claim(compared))
        return {'claims':claims,'comparisons':comparisons}

    def verify(self,state: AgentState) -> dict:
        try:
            verify_claims(state['claims'],state['evidence'])
            return {'fatal':False}
        except ValueError:
            return {'fatal':True,'codes':state['codes']+['claim_verification_failed']}

    async def compose(self,state: AgentState) -> dict:
        claims=state['claims']
        composition=None
        codes=state['codes']
        if claims:
            try:
                composition=await asyncio.wait_for(self.model.compose(claims),self.settings.llm_timeout_seconds)
                ids=composition.ordered_claim_ids
                if len(ids)!=len(claims) or set(ids)!={c.claim_id for c in claims}:
                    raise ValueError('Composer changed verified claim set')
                by_id={c.claim_id:c for c in claims}
                claims=[by_id[key] for key in ids]
            except Exception:
                composition=None
                codes=codes+['composition_fallback']
        response=self._response(state,claims,composition,codes)
        return {'response':response}

    def _response(self,state: AgentState,claims: list[Claim],composition: Composition | None,
                  codes: list[str]) -> QueryResponse:
        request=state['request']
        evidence=state['evidence']
        # Unknown source completeness makes observed-count answers partial even
        # when database execution succeeds. No failed value is replaced by zero.
        status='partial' if claims else 'error'
        limits=list(dict.fromkeys([c for e in evidence for c in e.caveats]))
        for comparison in state.get('comparisons',[]):
            limits.extend(x for x in comparison.limitations if x not in limits)
        errors=[e.error_code for e in evidence if e.error_code]
        comparison=Comparison(locations=[loc.id for loc in request.locations],radius_meters=request.radius_meters,
            time_range=request.time_range,metrics=state.get('comparisons',[])) if len(request.locations)==2 else None
        return QueryResponse(status=status,intent=state['plan'].intent,
            answer=render_answer(request,claims,state['unsupported'],composition),comparison=comparison,
            claims=claims,evidence=evidence,limitations=limits,
            limitation_codes=list(dict.fromkeys(codes+errors+['coverage_unknown','freshness_unknown'])),
            unsupported_dimensions=state['unsupported'],tool_trace_id=state['trace_id'])

    def final_validate(self,state: AgentState) -> dict:
        try:
            response=QueryResponse.model_validate(state['response'].model_dump())
            verify_claims(response.claims,response.evidence)
            # Re-render the only permitted free-text shape; compare calculations
            # independently, including machine-readable comparison payloads.
            for metric in (response.comparison.metrics if response.comparison else []):
                index={e.evidence_id:e for e in response.evidence}
                if compare_pair(*(index[key] for key in metric.evidence_ids))!=metric:
                    raise ValueError('Comparison payload differs from evidence')
            allowed=[render_answer(state['request'],response.claims,response.unsupported_dimensions,
                        Composition(ordered_claim_ids=[],introduction=style)) for style in INTRODUCTIONS]
            if response.answer not in allowed:
                raise ValueError('Unverified answer prose')
            response.execution_plan=[call.model_dump(mode='json') for call in state.get('calls',[])]
            response.tool_calls=[tool_log(result) for result in state.get('results',[])]
            return {'response':response}
        except (ValueError,KeyError):
            return self.abstain({**state,'fatal':True,'codes':state['codes']+['final_validation_failed']})

    def abstain(self,state: AgentState) -> dict:
        fatal=state.get('fatal',False)
        intent=state['plan'].intent if state.get('plan') else 'unsupported_dimension'
        text=('The request could not be completed and verified. No numerical conclusion is available.'
              if fatal else 'The requested dimensions are unavailable in Phase 0; no proxy statistics were substituted.')
        return {'response':QueryResponse(status='error' if fatal else 'unsupported',intent=intent,
            answer=text,evidence=state.get('evidence',[]),unsupported_dimensions=state['unsupported'],
            limitations=[text],limitation_codes=state['codes'] if fatal else ['unsupported_dimension'],
            tool_trace_id=state['trace_id'],
            execution_plan=[call.model_dump(mode='json') for call in state.get('calls',[])],
            tool_calls=[tool_log(result) for result in state.get('results',[])])}
