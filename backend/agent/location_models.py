"""Additive conversational contracts; coordinates are never LLM output fields."""
from typing import Literal
from pydantic import Field, model_validator
from datetime import date

from .models import Model,Plan,PlanOutput,TimeRange,QueryResponse,Location
from ..explorer_models import ViewportContext, MapVisualization


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
    entity_type: Literal['street_address','building_or_poi','intersection','region','coordinates'] = 'street_address'
    location_text: str | None = Field(default=None, max_length=240)

    @property
    def street(self):
        return ' '.join(x.strip() for x in [self.direction,self.street_name,self.street_suffix,self.post_direction] if x and x.strip())

    @property
    def display(self):
        if self.entity_type != 'street_address':
            return self.location_text or self.source_quote
        return ', '.join(x for x in [' '.join(x for x in [self.house_number,self.street] if x),
                                    self.city or 'San Diego',' '.join(x for x in [self.state or 'CA',self.postal_code] if x)] if x)


class ExtractedTimeRange(Model):
    start: str
    end: str
    calendar_timezone: str

    @model_validator(mode='after')
    def localize_naive_timestamps(self):
        """Models sometimes omit the UTC offset ("2026-01-01T00:00:00").
        Interpret such wall-clock values in the stated calendar timezone,
        including the correct DST offset, instead of failing the whole turn."""
        from datetime import datetime
        from zoneinfo import ZoneInfo
        try:
            zone=ZoneInfo(self.calendar_timezone)
        except Exception:
            return self  # TimeRange validation rejects an unknown zone later.
        for name in ('start','end'):
            try:
                value=datetime.fromisoformat(getattr(self,name))
            except ValueError:
                continue
            if value.tzinfo is None:
                object.__setattr__(self,name,value.replace(tzinfo=zone).isoformat())
        return self


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


class ConfirmationEdits(Model):
    start: date | None = None
    through: date | None = None
    radius_meters: float | None = Field(default=None, ge=0, le=50000)

    @model_validator(mode='after')
    def complete_interval(self):
        if (self.start is None) != (self.through is None) or (self.start and self.start > self.through):
            raise ValueError('Confirmation date edits require an increasing start/through pair.')
        return self


class ManualPoint(Model):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    source: Literal['user_coordinates','manual_open_map']
    confirmed: Literal[True]


class PlaceChoice(Model):
    place_id: str = Field(min_length=1, max_length=250, pattern=r'^[A-Za-z0-9_-]+$')
    search_token: str = Field(pattern=r'^[0-9a-f]{32}$')


class ChatTurn(Model):
    action: Literal['ask','confirm','correct','clarify','cancel','choose_candidates','choose_place','select_manual_locations']='ask'
    message: str | None=Field(default=None,max_length=4000)
    conversation_id: str | None=Field(default=None,max_length=100)
    session_id: str | None=Field(default=None,pattern=r'^[0-9a-f]{32}$')
    revision: int | None=Field(default=None,ge=1)
    # Supplied UI values are fallbacks only when the question omits scope.
    time_range: TimeRange | None=None
    radius_meters: float=Field(default=800,ge=0,le=50000)
    preferences: list[str]=Field(default_factory=list,max_length=8)
    candidate_choices: dict[str,str]=Field(default_factory=dict,max_length=2)
    viewport_context: ViewportContext | None = None
    confirmation_policy: Literal['always','conditional'] = 'always'
    confirmation_edits: ConfirmationEdits | None = None
    manual_points: dict[str,ManualPoint] = Field(default_factory=dict,max_length=2)
    place_choices: dict[str,PlaceChoice] = Field(default_factory=dict,max_length=2)


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
    session_id: str | None=None
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
    viewport_analysis: dict | None = None
    visualization: MapVisualization | None = None
    selected_place_ids: dict[str,str] = Field(default_factory=dict)
    confirmation_edits: ConfirmationEdits | None = None
