"""Additive conversational contracts; coordinates are never LLM output fields."""
from typing import Literal
from pydantic import Field

from .models import Model,Plan,PlanOutput,TimeRange,QueryResponse,Location


class AddressProposal(Model):
    id: Literal['A','B']
    source_quote: str
    house_number: str | None
    street_name: str | None
    street_suffix: str | None
    direction: str | None
    city: str | None
    state: str | None
    postal_code: str | None=Field(default=None,pattern=r'^\d{5}(?:-\d{4})?$')
    post_direction: str | None=None

    @property
    def street(self):
        return ' '.join(x.strip() for x in [self.direction,self.street_name,self.street_suffix,self.post_direction] if x and x.strip())

    @property
    def display(self):
        return ', '.join(x for x in [' '.join(x for x in [self.house_number,self.street] if x),
                                    self.city or 'San Diego',' '.join(x for x in [self.state or 'CA',self.postal_code] if x)] if x)


class ExtractedTimeRange(Model):
    start: str
    end: str
    calendar_timezone: str


class InterpretationOutput(Model):
    locations: list[AddressProposal]
    time_range: ExtractedTimeRange | None
    radius_meters: float | None
    plan: PlanOutput
    clarification: str | None
    unsupported_filters: list[str]


class AddressCorrectionOutput(Model):
    locations: list[AddressProposal]
    clarification: str | None


class ChatTurn(Model):
    action: Literal['ask','confirm','correct','clarify','cancel','choose_candidates']='ask'
    message: str | None=Field(default=None,max_length=4000)
    conversation_id: str | None=Field(default=None,max_length=100)
    revision: int | None=Field(default=None,ge=1)
    # Supplied UI values are fallbacks only when the question omits scope.
    time_range: TimeRange | None=None
    radius_meters: float=Field(default=800,ge=0,le=50000)
    preferences: list[str]=Field(default_factory=list,max_length=8)
    candidate_choices: dict[str,str]=Field(default_factory=dict,max_length=2)


class GeocodedCandidate(Model):
    candidate_id: str
    matched_address: str
    location: Location
    provider: str='US Census Geocoder'
    precision: str='interpolated_address'
    benchmark: str | None=None
    requires_confirmation: bool=False
    mismatch_reason: str | None=None
    metadata: dict=Field(default_factory=dict)


class ChatResponse(Model):
    status: Literal['needs_confirmation','needs_clarification','needs_candidate_confirmation',
                    'geocoding_failed','complete','cancelled','error']
    message: str
    conversation_id: str | None=None
    revision: int | None=None
    original_question: str | None=None
    locations: list[AddressProposal]=Field(default_factory=list)
    candidates: dict[str,list[GeocodedCandidate]]=Field(default_factory=dict)
    resolved_locations: list[GeocodedCandidate]=Field(default_factory=list)
    radius_meters: float | None=None
    time_range: TimeRange | None=None
    proposed_plan: Plan | None=None
    result: QueryResponse | None=None
    diagnostic_codes: list[str]=Field(default_factory=list)
    geocoding_diagnostics: dict[str,dict]=Field(default_factory=dict)
