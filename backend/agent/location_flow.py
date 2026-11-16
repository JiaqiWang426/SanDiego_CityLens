"""Bounded pending conversation state used by the existing Agent graph.

Process-local memory, opaque IDs, revision checks, per-conversation locks and
30-minute expiry. No database writes or coordinate generation by a model.
"""
import asyncio
from dataclasses import dataclass,field
from datetime import datetime
import re
import time
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi import HTTPException

from ..census_addresses import census_street
from .diagnostics import log_failure,model_failure_code
from .location_models import (AddressCorrectionOutput,ChatResponse,InterpretationOutput)
from .models import Plan,QueryRequest,TimeRange
from .llm import ModelNotConfigured
from .dimensions import chat_preferences


@dataclass
class PendingRequest:
    id: str
    question: str
    preferences: list
    revision: int=1
    updated: float=field(default_factory=time.monotonic)
    lock: asyncio.Lock=field(default_factory=asyncio.Lock)
    stage: str='clarify'
    analysis_question: str=''
    source_text: str=''
    locations: list=field(default_factory=list)
    radius: float=800
    time_range: TimeRange | None=None
    plan: Plan | None=None
    candidates: dict=field(default_factory=dict)
    resolved: dict=field(default_factory=dict)
    reply: ChatResponse | None=None
    diagnostic_codes: list=field(default_factory=list)
    address_clarification: bool=False
    geocoding_diagnostics: dict=field(default_factory=dict)
    viewport_context: dict | None=None
    confirmation_policy: str='always'
    verified_locations: dict=field(default_factory=dict)
    selected_place_ids: dict=field(default_factory=dict)
    confirmation_edits: dict | None=None
    query_context: dict | None=None


class LocationConversations:
    def __init__(self,geocoder,ttl=1800,capacity=200):
        self.geocoder,self.ttl,self.capacity=geocoder,ttl,capacity
        self.pending={}

    def session(self,turn):
        now=time.monotonic()
        for key,value in list(self.pending.items()):
            if now-value.updated>self.ttl and not value.lock.locked():
                del self.pending[key]
        if turn.action=='ask':
            if not turn.message or not turn.message.strip():
                raise HTTPException(422,'A question is required')
            if len(self.pending)>=self.capacity:
                raise HTTPException(503,'Conversation capacity reached; try again later')
            session=PendingRequest('conversation_'+uuid4().hex,turn.message,list(turn.preferences),
                analysis_question=turn.message,source_text=turn.message,radius=turn.radius_meters,time_range=turn.time_range,
                confirmation_policy=turn.confirmation_policy)
            self.pending[session.id]=session
            return session
        session=self.pending.get(turn.conversation_id)
        if session is None:
            raise HTTPException(409,'No valid pending request; submit the full question again')
        return session

    def reply(self,session,status,message):
        return ChatResponse(status=status,message=message,conversation_id=session.id,revision=session.revision,
            original_question=session.question,locations=session.locations,candidates=session.candidates,
            resolved_locations=[session.resolved[a.id] for a in session.locations if a.id in session.resolved],radius_meters=session.radius,
            time_range=session.time_range,proposed_plan=session.plan,diagnostic_codes=session.diagnostic_codes,
            geocoding_diagnostics=session.geocoding_diagnostics,selected_place_ids=session.selected_place_ids,
            confirmation_edits=session.confirmation_edits)

    def check_addresses(self,proposals,source):
        if not 1<=len(proposals)<=2 or len({a.id for a in proposals})!=len(proposals):
            raise ValueError('Provide one or two distinct location references')
        if [a.id for a in proposals]!=['A','B'][:len(proposals)]:
            raise ValueError('Location identifiers must be A then B')
        ready=True
        for address in proposals:
            if not address.source_quote or not address.source_quote.strip() or address.source_quote not in source:
                raise ValueError('Address interpretation lacks an exact user quote')
            from .user_locations import normalized_number_quote, coordinate_candidate
            if (address.entity_type == 'street_address' and normalized_number_quote(address.source_quote) != address.source_quote
                    and not re.search(r'\b(?:street address|address is)\b|\baddress\s*:',source,re.I)):
                # "One Miramar Street" may name a building. Treat it as a named
                # place; its numbered-street reading is offered only for confirmation.
                address.entity_type='building_or_poi'
                address.location_text=address.source_quote
            if address.entity_type != 'street_address':
                if address.location_text and address.location_text != address.source_quote:
                    raise ValueError('The original location expression must be preserved.')
                # A named place is searched by its original expression only.
                # Discard (never use) components a model invented for it.
                for name in ('house_number','street_name','street_suffix','direction','post_direction','postal_code'):
                    setattr(address,name,None)
                if address.entity_type == 'coordinates':coordinate_candidate(address)
                continue
            source_quote=normalized_number_quote(address.source_quote)
            if source_quote != address.source_quote and not re.search(r'\b(?:street address|address is)\b|\baddress\s*:',source,re.I):
                raise ValueError('A written-number expression may be a building name; classify it before assuming a house number.')
            if not address.house_number or not address.street_name:
                ready=False
                continue
            if (not re.fullmatch(r'[1-9][0-9]{0,6}',address.house_number)
                    or not re.match(r'\s*'+re.escape(address.house_number)+r'\b',source_quote)):
                raise ValueError('A positive house number must appear at the start of its quoted address')
            # Protect the actual name; suffix typo interpretation remains the LLM's
            # proposed edit, shown to the user before any geographic request.
            source_street=census_street(re.sub(r'^\s*'+re.escape(address.house_number)+r'\s+',
                '',source_quote.split(',')[0]))
            name=census_street(address.street_name)
            quote=source_street
            if not re.search(r'\b'+re.escape(name)+r'\b',quote):
                raise ValueError('Street name is not supported by the user quote')
            prefix=census_street(' '.join(x for x in [address.direction,address.street_name] if x))
            if not re.match(re.escape(prefix)+r'(?:\s|$)',source_street):
                raise ValueError('A direction or street-name component was added or discarded')
            remainder=source_street[len(prefix):].strip()
            if address.post_direction:
                post=census_street(address.post_direction)
                if not remainder or remainder.split()[-1]!=post:
                    raise ValueError('A post-direction was added or changed')
                remainder=' '.join(remainder.split()[:-1])
            if bool(address.street_suffix)!=bool(remainder) or len(remainder.split())>1:
                raise ValueError('A suffix or meaningful address component was added or discarded')
            if (address.state or 'CA').upper()!='CA' or (address.city or 'San Diego').upper() not in {'SAN DIEGO','LA JOLLA'}:
                raise ValueError('Only San Diego or La Jolla, CA addresses are supported')
            # San Diego is the application default. Another postal city changes
            # provider lookups, so it must come from the user, never from
            # branding such as "Axiom La Jolla" or a remembered neighborhood.
            city=' '.join((address.city or 'San Diego').split())
            if city.upper()!='SAN DIEGO' and not re.search(r'\b'+r'\s+'.join(map(re.escape,city.split()))+r'\b',source,re.I):
                raise ValueError('A non-default postal city must be supported by user text')
            if address.postal_code and not re.search(r'\b'+re.escape(address.postal_code)+r'\b',source):
                raise ValueError('A ZIP code must be supported by user text')
        return ready

    @staticmethod
    def can_auto_resolve(session):
        from .user_geocoding import interactive_street
        for address in session.locations:
            if address.entity_type != 'street_address':continue
            original=address.source_quote.split(',')[0]
            if interactive_street(original) != interactive_street(address.house_number+' '+address.street):
                return False
        return bool(session.locations)

    @staticmethod
    def apply_confirmation_edits(session,turn):
        if not turn.confirmation_edits:return
        if turn.action not in {'confirm','choose_candidates','select_manual_locations'}:
            raise HTTPException(422,'Scope edits require an explicit current-request confirmation.')
        from datetime import datetime,time,timedelta
        edits=turn.confirmation_edits
        if edits.start:
            session.time_range=TimeRange(start=datetime.combine(edits.start,time.min,ZoneInfo('America/Los_Angeles')),
                end=datetime.combine(edits.through+timedelta(days=1),time.min,ZoneInfo('America/Los_Angeles')))
        if edits.radius_meters is not None:session.radius=edits.radius_meters
        session.confirmation_edits=edits.model_dump(mode='json')

    async def interpret(self,agent,session,turn,correction=False):
        session.diagnostic_codes=[]
        session.address_clarification=False
        stage='provider'
        try:
            if correction:
                raw=await asyncio.wait_for(agent.model.correct_locations(turn.message,session.locations),agent.settings.llm_timeout_seconds)
                stage='interpretation_validation'
                output=AddressCorrectionOutput.model_validate(raw.model_dump())
                if [a.id for a in output.locations]!=[a.id for a in session.locations]:
                    raise ValueError('Corrections must preserve existing location identifiers')
            else:
                session.preferences=chat_preferences(session.analysis_question,session.preferences)
                defaults=dict(radius_meters=turn.radius_meters,time_range=turn.time_range.model_dump(mode='json') if turn.time_range else None,
                              preferences=session.preferences)
                raw=await asyncio.wait_for(agent.model.interpret_locations(session.analysis_question,defaults,
                    datetime.now(ZoneInfo('America/Los_Angeles')).isoformat()),agent.settings.llm_timeout_seconds)
                stage='interpretation_validation'
                output=InterpretationOutput.model_validate(raw.model_dump())
                session.plan=Plan.model_validate(output.plan.model_dump())
                session.time_range=TimeRange.model_validate(output.time_range.model_dump()) if output.time_range else turn.time_range
                radius=output.radius_meters if output.radius_meters is not None else turn.radius_meters
                if not 0<=radius<=50000:
                    raise ValueError('Radius must be 0 through 50000 meters')
                session.radius=radius
                from .scope_resolution import resolve_scope
                session.time_range, session.radius = resolve_scope(session.analysis_question, session.time_range, session.radius,
                    system_defaults=session.confirmation_policy == 'conditional')
                if output.unsupported_filters:
                    session.stage='clarify'
                    return self.reply(session,'needs_clarification','These filters are unavailable and were not applied: '+', '.join(output.unsupported_filters)+'. Revise the question.')
            ready=self.check_addresses(output.locations,session.source_text)
            for old in session.locations:
                if old.id in session.resolved:
                    session.verified_locations[old.display.casefold()] = session.resolved[old.id].model_dump(mode='json')
            session.locations=output.locations
            session.candidates={}
            session.resolved={}
            session.geocoding_diagnostics={}
            if output.clarification or not ready or not session.time_range:
                session.address_clarification=bool(session.time_range and session.plan and
                    (output.clarification or not ready))
                session.stage='clarify'
                return self.reply(session,'needs_clarification',output.clarification or
                    'Please provide a specific street number/name for each location and a time interval.')
            session.stage='confirm_addresses'
            return self.reply(session,'needs_confirmation',
                'Please confirm these proposed address interpretations and query conditions. Addresses have not yet been geographically verified.')
        except Exception as exc:
            code=('openai_not_configured' if isinstance(exc,ModelNotConfigured) else
                'location_interpretation_rejected' if stage=='interpretation_validation' else
                model_failure_code(exc,'location_interpretation',stage))
            log_failure(code,exc,stage=stage,secrets=(agent.settings.openai_api_key,))
            session.stage='clarify'
            session.diagnostic_codes=[code]
            if stage=='provider':
                return self.reply(session,'error',
                    'The language-model interpretation failed. No geocoding or analysis ran. Check diagnostics and retry the full question.')
            return self.reply(session,'needs_clarification',
                'I could not establish a specific supported address and query scope. Please clarify the street number/name and requested interval.')

    async def prepare(self,agent,session,turn):
        """Graph node: return a pending reply OR an authoritative pin request."""
        session.updated=time.monotonic()
        if turn.action!='ask' and turn.revision!=session.revision:
            raise HTTPException(409,'This proposal changed; use the latest confirmation revision')
        if session.stage in {'cancelled','complete'}:
            return {'conversation_reply':session.reply}
        self.apply_confirmation_edits(session,turn)
        if turn.action=='cancel':
            session.stage='cancelled'
            session.candidates={}
            session.resolved={}
            session.reply=self.reply(session,'cancelled','Request cancelled. No additional geocoding or analytical query will run.')
            return {'conversation_reply':session.reply}
        if turn.action in {'ask','correct','clarify'}:
            if not turn.message or not turn.message.strip():
                raise HTTPException(422,'A question or correction is required')
            if turn.action!='ask':
                session.revision+=1
                session.source_text+='\n'+turn.message
            address_correction=turn.action=='correct' or (turn.action=='clarify' and session.address_clarification)
            if turn.action=='clarify' and not address_correction:
                session.analysis_question+='\nClarification: '+turn.message
                # Keep established defaults if the clarification supplies no UI scope.
                turn=turn.model_copy(update={'time_range':session.time_range,'radius_meters':session.radius})
            reply=await self.interpret(agent,session,turn,correction=address_correction)
            if session.confirmation_policy == 'conditional' and reply.status == 'needs_confirmation' and self.can_auto_resolve(session):
                turn=turn.model_copy(update={'action':'confirm','revision':session.revision})
            else:
                return {'conversation_reply':reply}
        if turn.action=='select_manual_locations':
            if session.stage not in {'confirm_addresses','failed_geocoding','confirm_candidates','clarify'} or not session.plan or not session.time_range:
                raise HTTPException(409,'Establish the query scope before selecting locations.')
            required={a.id for a in session.locations}-set(session.resolved)
            if not required or set(turn.manual_points)!=required:
                raise HTTPException(422,'Explicitly select and confirm every unresolved location.')
            from .location_models import GeocodedCandidate
            from .models import Location
            from uuid import uuid4
            for key,point in turn.manual_points.items():
                session.resolved[key]=GeocodedCandidate(candidate_id='manual_'+uuid4().hex,
                    matched_address='User-selected point '+key,location=Location(id=key,latitude=point.latitude,longitude=point.longitude),
                    provider='User-selected point',precision='user_selected',metadata={
                        'coordinate_source':point.source,'user_confirmed':True,
                        'original_text':next(a.source_quote for a in session.locations if a.id==key)})
            session.candidates={}
        elif turn.action=='choose_place':
            from .places import discovery
            if session.stage not in {'failed_geocoding','confirm_candidates'} or not turn.place_choices:
                raise HTTPException(409,'No unresolved place search is awaiting selection.')
            for key,choice in turn.place_choices.items():
                original=next((a for a in session.locations if a.id==key),None)
                if key not in {a.id for a in session.locations}-set(session.resolved) or not discovery.validate_choice(choice.search_token,choice.place_id,original.location_text or original.source_quote):
                    raise HTTPException(422,'Place selection is expired or does not belong to a fresh search.')
                session.selected_place_ids[key]=choice.place_id
            session.stage='failed_geocoding';session.revision+=1
            return {'conversation_reply':self.reply(session,'geocoding_failed',
                'Place identity selected. Analytical coordinates still need an independent source. Enter an independently known street address, choose an independent candidate, or pick a point on the separate open map.')}
        elif turn.action=='confirm':
            if session.stage!='confirm_addresses':
                raise HTTPException(409,'No complete address proposal is awaiting confirmation')
            session.candidates={}
            session.resolved={}
            session.geocoding_diagnostics={}
            failures=[]
            for address in session.locations:
                try:
                    from .user_locations import analytical_candidate
                    from .user_geocoding import AddressResolution
                    from .location_models import GeocodedCandidate
                    reused=session.verified_locations.get(address.display.casefold())
                    if reused:
                        candidate=analytical_candidate(GeocodedCandidate.model_validate(reused))
                        candidate.location=candidate.location.model_copy(update={'id':address.id})
                        candidate.metadata={**candidate.metadata,'reused_confirmed_location':True}
                        resolution=AddressResolution('resolved',[candidate],{'provider':candidate.provider})
                    else:
                        resolution=await asyncio.to_thread(self.geocoder.resolve,address)
                        resolution.candidates=[analytical_candidate(c) for c in resolution.candidates]
                except Exception as exc:
                    log_failure('interactive_geocoder_failed',exc,stage='interactive_geocoding')
                    failures.append(address.id+': provider_error')
                    continue
                session.geocoding_diagnostics[address.id]=resolution.metadata
                if resolution.status=='resolved':
                    if len(resolution.candidates)!=1 or resolution.candidates[0].requires_confirmation:
                        raise HTTPException(409,'The provider must return one unambiguous acceptable location.')
                    resolution.candidates[0].metadata['acceptance_rule']='single_independent_match_or_explicit_user_coordinates'
                    session.resolved[address.id]=resolution.candidates[0]
                elif resolution.status=='needs_confirmation':
                    session.candidates[address.id]=resolution.candidates
                else:
                    failures.append(address.id+': '+resolution.status+
                        (' ('+resolution.metadata['provider']+')' if resolution.metadata.get('provider') else ''))
            if failures:
                session.stage='failed_geocoding'
                session.revision+=1
                session.diagnostic_codes=['location_unresolved:'+f for f in failures]
                names={a.id:a.display for a in session.locations}
                missing=', '.join('“'+names.get(f.split(':')[0],f)+'”' for f in failures)
                return {'conversation_reply':self.reply(session,'geocoding_failed',
                    f'I couldn’t find an independently verified location for {missing}. You can type its street address, '
                    'search Google Places to identify the place, or pick the point on the map. No analysis has run yet.')}
            if session.candidates:
                session.stage='confirm_candidates'
                session.revision+=1
                return {'conversation_reply':self.reply(session,'needs_candidate_confirmation',
                    'A provider returned multiple or materially different locations. Explicitly choose and confirm a candidate for each unresolved location. Property or interpolated points are not verified apartment-unit positions.')}
        elif turn.action=='choose_candidates':
            if session.stage!='confirm_candidates' or set(turn.candidate_choices)!=set(session.candidates):
                raise HTTPException(409,'Select a candidate for every pending location')
            selected_locations={}
            for id,choices in session.candidates.items():
                selected=next((c for c in choices if c.candidate_id==turn.candidate_choices[id]),None)
                if selected is None:
                    raise HTTPException(422,'Candidate does not belong to this pending location')
                selected_locations[id]=selected
                from .user_locations import analytical_candidate
                analytical_candidate(selected)
                selected.requires_confirmation=False
                selected.metadata={**selected.metadata,'user_confirmed':True}
            session.resolved.update(selected_locations)
            session.candidates={}
        else:
            raise HTTPException(409,'Invalid confirmation state')
        if len(session.resolved)!=len(session.locations):
            raise HTTPException(409,'Every required location must be resolved')
        session.stage='executing'
        request=QueryRequest(question=session.analysis_question,locations=[session.resolved[a.id].location for a in session.locations],
            time_range=session.time_range,radius_meters=session.radius,preferences=session.preferences,conversation_id=session.id)
        return {'request':request,'confirmed_plan':session.plan}
