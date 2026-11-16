"""Bounded LangGraph viewport branch; the established radius graph is unchanged."""
import asyncio
import re
from typing import Literal, TypedDict
from uuid import uuid4

from langgraph.graph import END, START, StateGraph

from ..explorer_models import MapModel, ViewportContext, MapVisualization
from ..explorer_store import ExplorerStore, UnsupportedMapFilter
from .location_models import ChatResponse


def requests_viewport(message):
    text = message or ''
    return bool(re.search(r'\b(?:current|visible)\s+map\b|\bmap\s+(?:view|viewport)\b|\b(?:shown|visible|displayed)\s+on\s+(?:the\s+)?map\b|当前地图|地图.{0,8}(?:当前|可见|显示)|当前.{0,8}(?:视野|地图)', text, re.I))


class ViewportPlanOutput(MapModel):
    mode: Literal['viewport', 'radius', 'clarify']
    dataset: Literal['service_requests', 'traffic_collisions'] | None
    start: str | None
    end: str | None
    category: str | None
    status: str | None
    community: str | None
    zipcode: str | None
    exclude_linked: bool | None
    change_quote: str | None
    clarification: str | None


VIEWPORT_PROMPT = '''Plan a CityLens map-view summary. Return only typed fields.
Never return coordinates, SQL, counts, estimates or analytical conclusions.
Use mode=viewport ONLY for an explicit current-map request or a clear follow-up
to the saved viewport analysis. Explicit addresses, named locations, comparisons,
or distance/radius constraints take precedence: choose radius and let the existing
address agent handle them. Ambiguous requests need clarify, never a proxy count.
Only imported service_requests and traffic_collisions counts/group summaries are
supported. Crime, safety, repairs, causal conclusions and rankings need clarify.
Viewport service requests cover only source council_district=1 during 2026;
collisions cover the imported 2026 dataset, not a verified District 1 polygon.
Keep the supplied bounds: there is no output field that can change them.
Null replacement fields inherit current filters. To clear a filter return an
empty string. Dates are inclusive YYYY-MM-DD within 2026. Every replacement needs
change_quote, an exact substring of the user message. Category/status/planning
area/ZIP labels must appear literally in that quote (case-insensitive). Do not
invent synonyms: unresolved is not an authoritative source status. Clarify if
the request cannot be represented. Collision status is GEOCODING status; it has
no planning-area/ZIP/linked-request fields. Do not drop unsupported constraints.
Dataset changes must be explicitly named in change_quote. Unrelated location
questions must choose radius, even if a viewport was supplied as UI context.
The current map filters are only authoritative for an explicit map request;
follow-ups retain SAVED filters/bounds unless a new current map is requested.'''


class MapState(TypedDict, total=False):
    message: str
    scope: ViewportContext
    plan: ViewportPlanOutput
    response: ChatResponse | None
    context: dict | None


class ViewportAnalysis:
    def __init__(self, agent):
        self.agent = agent
        builder = StateGraph(MapState)
        builder.add_node('validate_map_plan', self.plan)
        builder.add_node('query_and_verify_map_evidence', self.execute)
        builder.add_edge(START, 'validate_map_plan')
        builder.add_conditional_edges('validate_map_plan', lambda s: END if 'response' in s else 'query_and_verify_map_evidence')
        builder.add_edge('query_and_verify_map_evidence', END)
        self.graph = builder.compile(checkpointer=False)

    async def run(self, turn, saved=None):
        explicit = requests_viewport(turn.message)
        if not explicit and not (saved and saved.get('mode') == 'viewport'):
            return None, None
        scope = turn.viewport_context if explicit else ViewportContext.model_validate(saved['viewport'])
        if scope is None:
            return ChatResponse(status='needs_clarification', message='Open the Explorer and provide the current map view, or specify an address for radius analysis.',
                                diagnostic_codes=['viewport_context_required']), saved
        if re.search(r'\b(?:radius|within\s+\d|\d+(?:\.\d+)?\s*(?:meters?|metres?|miles?|km|m)\b)|半径|\d+\s*(?:米|公里)', turn.message or '', re.I):
            if not explicit:
                return None, None
            return ChatResponse(status='needs_clarification', message='A map rectangle and an address radius are distinct scopes. Please request the map view alone, or provide an address and radius for location analysis.', diagnostic_codes=['conflicting_spatial_scopes']), saved
        state = await self.graph.ainvoke({'message': turn.message or '', 'scope': scope})
        retained = {'mode': 'viewport', 'viewport': state['scope'].model_dump(mode='json')}
        return state.get('response'), state.get('context', retained)

    async def plan(self, state):
        try:
            output = await asyncio.wait_for(self.agent.model.plan_viewport(state['message'], state['scope'].model_dump(mode='json')),
                                            self.agent.settings.llm_timeout_seconds)
            plan = ViewportPlanOutput.model_validate(output.model_dump())
            if plan.mode == 'radius':
                return {'response': None, 'context': None}
            if plan.mode == 'clarify' or plan.clarification:
                return {'response': ChatResponse(status='needs_clarification', message=plan.clarification or 'Please clarify the map filters or supply an address.')}
            changes = {key: value for key, value in plan.model_dump().items()
                       if key in type(state['scope'].filters).model_fields and value is not None}
            if changes:
                quote = plan.change_quote
                if not quote or quote not in state['message']:
                    raise ValueError('Filter changes require an exact user quote.')
                for key in ['category', 'status', 'community', 'zipcode']:
                    if changes.get(key) and changes[key].casefold() not in quote.casefold():
                        raise ValueError('Filter label is absent from the user quote.')
                if 'dataset' in changes and changes['dataset'] != state['scope'].filters.dataset:
                    pattern = r'collision|crash|accident|碰撞|事故' if changes['dataset'] == 'traffic_collisions' else r'complaint|service request|get it done|pothole|投诉|报修'
                    if not re.search(pattern, quote, re.I):
                        raise ValueError('Dataset change lacks a supported user quote.')
            changes = {key: None if value == '' else value for key, value in changes.items()}
            values = state['scope'].filters.model_dump()
            values.update(changes)
            scope = ViewportContext(bounds=state['scope'].bounds, filters=values)
            return {'plan': plan, 'scope': scope}
        except Exception as exc:
            from .llm import ModelNotConfigured
            code = 'openai_not_configured' if isinstance(exc, ModelNotConfigured) else 'viewport_plan_rejected'
            return {'response': ChatResponse(status='needs_clarification', message='The map request could not be planned. Configure the model or clarify supported filters; no database counts were substituted.', diagnostic_codes=[code])}

    async def execute(self, state):
        scope = state['scope']
        try:
            summary = await asyncio.to_thread(ExplorerStore(self.agent.settings.database_url).summary, scope.filters, scope.bounds, validate_labels=True)
            scope = ViewportContext(bounds=scope.bounds, filters=summary.get('applied_filters', scope.filters.model_dump()))
            total = summary['total']
            if not isinstance(total, int) or total < 0 or any(
                sum(row['count'] or 0 for row in summary[key]) != total for key in ['categories', 'statuses', 'months']):
                raise ValueError('Aggregate evidence failed consistency verification')
            evidence_id = 'viewport_' + uuid4().hex
            evidence = dict(evidence_id=evidence_id, source_table='citylens.' + scope.filters.dataset,
                metric='located_report_count_in_viewport', value=total, scope=scope.model_dump(mode='json'),
                summary=summary, limitations=[
                    'The rectangle includes its boundary; reports without coordinates cannot be assigned to this view.',
                    'Counts represent imported reports, not physical issues, repair outcomes or safety.',
                    'Service requests are scoped to source District 1; collisions use the imported dataset.',
                    'Source completeness and freshness are unknown; follow-ups execute new read-only queries.'])
            visualization = MapVisualization(mode='viewport', bounds=scope.bounds,
                datasets=[scope.filters.dataset], evidence_ids=[evidence_id])
            response = ChatResponse(status='complete', message=f'The supplied map view contains {total:,} located {scope.filters.dataset.replace("_", " ")} reports matching the verified filters.',
                viewport_analysis=evidence, visualization=visualization)
            return {'response': response, 'context': {'mode': 'viewport', 'viewport': scope.model_dump(mode='json')}}
        except UnsupportedMapFilter:
            return {'response': ChatResponse(status='needs_clarification', message='The requested source labels are unavailable or the query evidence could not be verified. Please select an exact issue type/status or clarify the filters.', diagnostic_codes=['viewport_filter_or_evidence_rejected'])}
        except Exception:
            return {'response': ChatResponse(status='error', message='The viewport query could not be executed and verified. No count is available.', diagnostic_codes=['viewport_database_unavailable_or_verification_failed'])}


def radius_visualization(evidence):
    from ..explorer_models import MapCircle
    circles = {}
    for record in evidence:
        if record.status not in {'ok', 'partial'} or record.value is None:
            continue
        scope = record.scope
        key = (record.location_id, scope.latitude, scope.longitude, scope.radius_meters)
        if key not in circles:
            circles[key] = MapCircle(location_id=record.location_id, latitude=scope.latitude,
                longitude=scope.longitude, radius_meters=scope.radius_meters, datasets=[], evidence_ids=[])
        circles[key].datasets = list(dict.fromkeys([*circles[key].datasets, record.domain]))
        circles[key].evidence_ids.append(record.evidence_id)
    return MapVisualization(mode='radius', circles=list(circles.values()),
        datasets=list(dict.fromkeys(domain for circle in circles.values() for domain in circle.datasets)),
        evidence_ids=[key for circle in circles.values() for key in circle.evidence_ids]) if circles else None
