"""Durable conversation contracts; only explicit follow-up fields are patched."""
from datetime import datetime
from typing import Literal

from pydantic import Field

from .location_models import ExtractedTimeRange
from .models import Model, PlanOutput


PlanChangeField = Literal['intent', 'tools', 'period', 'exclude_linked_duplicates',
    'unsupported_dimensions', 'service_request_aggregation.group_by',
    'service_request_aggregation.service_name', 'service_request_aggregation.status',
    'service_request_aggregation.service_names',
    'collision_filters.min_injured', 'collision_filters.max_injured',
    'collision_filters.min_killed', 'collision_filters.max_killed',
    'collision_filters.hit_run', 'collision_filters.street_name', 'collision_filters.hit_run_level']


class SessionCreate(Model):
    title: str = Field(default='New conversation', min_length=1, max_length=100)


class SessionMetadata(Model):
    session_id: str
    title: str
    created_at: datetime
    updated_at: datetime


class FollowupOutput(Model):
    # A complete plan uses the existing planner schema. Only named leaves below
    # may replace saved values; model defaults never erase established filters.
    mode: Literal['followup', 'new_locations', 'clarify']
    radius_meters: float | None
    radius_quote: str | None
    time_range: ExtractedTimeRange | None
    time_quote: str | None
    plan: PlanOutput
    changed_plan_fields: list[PlanChangeField]
    change_quote: str | None
    clarification: str | None
