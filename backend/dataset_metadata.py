"""Read-only dataset metadata with distinct event/import/source timestamps."""
from typing import Literal

from pydantic import BaseModel,ConfigDict,Field,AwareDatetime,model_validator

if __package__:
    from .collision_queries import _run
else:
    from collision_queries import _run

DatasetDomain=Literal['service_requests','traffic_collisions']


class DatasetMetadata(BaseModel):
    model_config=ConfigDict(extra='forbid')
    domain: DatasetDomain
    dataset_id: str
    source_url: str
    dataset_records: int=Field(ge=0)
    dataset_located_records: int=Field(ge=0)
    dataset_unlocated_records: int=Field(ge=0)
    dataset_dated_records: int=Field(ge=0)
    dataset_observed_start: AwareDatetime | None=None
    dataset_observed_end: AwareDatetime | None=None
    last_successful_local_ingestion_at: AwareDatetime | None=None
    source_updated_at: AwareDatetime | None=None
    source_publication_at: AwareDatetime | None=None
    source_freshness_verified: bool=False
    source_timestamp_status: str
    coverage_verified: bool=False
    coverage_scope: str
    coordinate_presence_definition: str
    geocoding_coverage: float | None=Field(default=None,ge=0,le=1)
    coordinate_coverage: float | None=Field(default=None,ge=0,le=1)
    geocode_status_breakdown: dict[str,int] | None=None
    geocode_precision_breakdown: dict[str,int] | None=None
    spatial_accuracy: str
    normalization_policy: str
    local_ingestion_timestamp_definition: str
    latest_import_id: int | None=None
    dataset_imports: list[dict]=Field(default_factory=list)
    dataset_imports_limit: int=100
    dataset_imports_truncated: bool=False
    limitations: list[str]

    @model_validator(mode='after')
    def consistent_counts(self):
        if self.dataset_located_records+self.dataset_unlocated_records!=self.dataset_records:
            raise ValueError('Coordinate counts do not reconcile')
        if self.dataset_dated_records>self.dataset_records:
            raise ValueError('Dated count exceeds dataset count')
        if self.geocode_status_breakdown is not None and sum(self.geocode_status_breakdown.values())!=self.dataset_records:
            raise ValueError('Geocoding statuses do not reconcile')
        return self


def get_dataset_metadata(conn,domain: DatasetDomain) -> DatasetMetadata:
    """Read metadata for one whole imported domain, not a local neighborhood."""
    if domain not in ('service_requests','traffic_collisions'):
        raise ValueError('Unsupported dataset domain')
    rows=_run(conn,'SELECT citylens.get_dataset_metadata(%s) AS metadata',(domain,))
    return DatasetMetadata.model_validate(rows[0]['metadata'])
