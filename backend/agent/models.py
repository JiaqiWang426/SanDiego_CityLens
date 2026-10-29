"""Public contracts and validated internal plans. Unknown is distinct from zero."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, AwareDatetime, model_validator

Domain = Literal['service_requests', 'traffic_collisions']
ToolName = Literal['count_service_requests', 'get_traffic_collisions', 'aggregate_traffic_collisions']
Intent = Literal['location_summary', 'compare_locations', 'collision_trend',
                 'collision_drilldown', 'request_count', 'unsupported_dimension']
Period = Literal['day', 'week', 'month', 'quarter', 'year']
UnsupportedDimension = Literal['crime','safety','commute','amenities','development',
    'rental_ranking','service_category_breakdown','service_status_breakdown','unavailable_dimension']


class Model(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)


class Location(Model):
    id: str = Field(min_length=1, max_length=32, pattern=r'^[A-Za-z0-9_-]+$')
    label: str | None = Field(default=None, max_length=100)
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)

    @model_validator(mode='after')
    def supported_area(self):
        # Deliberately only an admission envelope, not a verified city polygon.
        if not (32.5 <= self.latitude <= 33.2 and -117.6 <= self.longitude <= -116.8):
            raise ValueError('Phase 0 accepts San Diego-area pins only (approximate admission envelope)')
        return self


class TimeRange(Model):
    start: AwareDatetime
    end: AwareDatetime
    calendar_timezone: str = 'America/Los_Angeles'

    @model_validator(mode='after')
    def valid_interval(self):
        if not self.start < self.end or self.end - self.start > timedelta(days=3660):
            raise ValueError('Time range must be increasing and at most 3660 days')
        try:
            ZoneInfo(self.calendar_timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError('Unknown IANA calendar timezone') from None
        return self


class QueryRequest(Model):
    question: str = Field(min_length=1, max_length=4000)
    locations: list[Location] = Field(min_length=1, max_length=2)
    radius_meters: float = Field(default=800, ge=0, le=50000)
    time_range: TimeRange
    preferences: list[str] = Field(default_factory=list, max_length=8)
    conversation_id: str | None = Field(default=None, max_length=100)

    @model_validator(mode='after')
    def valid_names(self):
        if not self.question.strip():
            raise ValueError('Question cannot be blank')
        if len({loc.id for loc in self.locations}) != len(self.locations):
            raise ValueError('Location IDs must be distinct')
        if any(not 1 <= len(p) <= 80 for p in self.preferences):
            raise ValueError('Preference names must be 1..80 characters')
        return self


class CollisionFilters(Model):
    min_injured: int | None = Field(default=None, ge=0, le=2147483647)
    max_injured: int | None = Field(default=None, ge=0, le=2147483647)
    min_killed: int | None = Field(default=None, ge=0, le=2147483647)
    max_killed: int | None = Field(default=None, ge=0, le=2147483647)
    hit_run: bool | None = None
    street_name: str | None = Field(default=None, min_length=1, max_length=120)
    hit_run_level: Literal['FELONY', 'MISDEMEANOR'] | None = None

    @model_validator(mode='after')
    def bounds(self):
        for low, high in [(self.min_injured,self.max_injured),(self.min_killed,self.max_killed)]:
            if low is not None and high is not None and low > high:
                raise ValueError('Minimum exceeds maximum')
        return self


class Plan(Model):
    """LLM chooses tools, not database statements or frontend-supplied scope."""
    intent: Intent
    tools: list[ToolName] = Field(max_length=3)
    period: Period = 'month'
    collision_filters: CollisionFilters = Field(default_factory=CollisionFilters)
    exclude_linked_duplicates: bool = False
    filter_quote: str | None = Field(default=None, max_length=500)
    unsupported_dimensions: list[UnsupportedDimension] = Field(default_factory=list, max_length=8)


class Scope(Model):
    latitude: float
    longitude: float
    radius_meters: float
    start_inclusive: AwareDatetime
    end_exclusive: AwareDatetime
    calendar_timezone: str


class Provenance(Model):
    # Exact contributors are not returned by existing aggregate SQL functions.
    import_ids: list[int] | None = None
    source_file_sha256: str | None = None
    source_freshness_verified: bool = False
    data_as_of: AwareDatetime | None = None
    dataset_import_ids: list[int] = Field(default_factory=list)
    dataset_imports: list[dict] = Field(default_factory=list)
    dataset_imports_truncated: bool = False
    snapshot_policy: str = 'upsert_current_records_not_historical_snapshot'


class Quality(Model):
    completeness: Literal['unknown'] = 'unknown'
    coordinate_precision: str = 'unknown'
    unlocated_within_radius: Literal['not_determinable'] = 'not_determinable'
    dataset_records: int | None = None
    dataset_located_records: int | None = None
    dataset_dated_records: int | None = None
    dataset_observed_start: AwareDatetime | None = None
    dataset_observed_end: AwareDatetime | None = None
    # These are dataset-wide diagnostics, not neighborhood coverage estimates.


class Bucket(Model):
    period_start: AwareDatetime
    period_end: AwareDatetime
    report_count: int = Field(ge=0)
    located_count: int = Field(ge=0)
    unknown_injury_count: int = Field(ge=0)
    unknown_fatality_count: int = Field(ge=0)
    source_bucket_present: bool

    @model_validator(mode='after')
    def consistent_counts(self):
        if any(count>self.report_count for count in (
            self.located_count,self.unknown_injury_count,self.unknown_fatality_count)):
            raise ValueError('Bucket diagnostic count exceeds report count')
        return self


class EvidenceRecord(Model):
    evidence_id: str
    query_id: str
    domain: Domain
    source_table: str
    source_dataset: str
    source_url: str
    tool: ToolName
    tool_version: str = '1'
    metric: Literal['request_id_count', 'geocoded_collision_report_count', 'retrieved_collision_page_size']
    metric_definition: str
    value: int | None = Field(ge=0)
    unit: Literal['reports'] = 'reports'
    location_id: str
    scope: Scope
    filters: dict
    provenance: Provenance
    quality: Quality
    status: Literal['ok','partial','unavailable','error']
    executed_at: AwareDatetime
    elapsed_ms: float = Field(ge=0)
    buckets: list[Bucket] = Field(default_factory=list)
    records: list[dict] = Field(default_factory=list)
    pagination: dict | None = None
    caveats: list[str]
    error_code: str | None = None


class ComparisonMetric(Model):
    metric: str
    location_a: str
    location_b: str
    value_a: int
    value_b: int
    difference_b_minus_a: int
    evidence_ids: list[str]
    overlapping_circles: bool
    limitations: list[str]


class Comparison(Model):
    locations: list[str]
    radius_meters: float
    time_range: TimeRange
    metrics: list[ComparisonMetric]


class Claim(Model):
    claim_id: str
    text: str
    claim_type: Literal['record_count','page_size','descriptive_comparison']
    evidence_ids: list[str]
    metric: str
    value: int
    limitations: list[str]


class Composition(Model):
    """Model orders verified facts; it cannot introduce unchecked free prose."""
    ordered_claim_ids: list[str]
    introduction: Literal['records_only','comparison_only','trend_only']


class ToolLog(Model):
    """Executed operations, not model reasoning or a proposed call list."""
    query_id: str
    tool: ToolName
    executed_at: AwareDatetime
    elapsed_ms: float
    status: str
    error_code: str | None = None
    arguments: dict
    parameterized_sql: str
    parameters: list
    read_only_sql: str
    result_rows: list[dict]


class QueryResponse(Model):
    status: Literal['ok','partial','needs_location','unsupported','error']
    intent: Intent
    answer: str
    comparison: Comparison | None = None
    claims: list[Claim] = Field(default_factory=list)
    evidence: list[EvidenceRecord] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    limitation_codes: list[str] = Field(default_factory=list)
    unsupported_dimensions: list[str] = Field(default_factory=list)
    tool_trace_id: str
    execution_plan: list[dict] = Field(default_factory=list)
    tool_calls: list[ToolLog] = Field(default_factory=list)
