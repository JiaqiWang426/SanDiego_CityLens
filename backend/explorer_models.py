"""Validated map scope. Bounds never come from model output."""
from datetime import date
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


class MapModel(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)


class Bounds(MapModel):
    west: float = Field(ge=-180, le=180)
    south: float = Field(ge=-90, le=90)
    east: float = Field(ge=-180, le=180)
    north: float = Field(ge=-90, le=90)

    @model_validator(mode='after')
    def increasing(self):
        if self.west >= self.east or self.south >= self.north:
            raise ValueError('Map bounds must increase and cannot cross the antimeridian.')
        return self


class ExplorerFilters(MapModel):
    dataset: Literal['service_requests', 'traffic_collisions'] = 'service_requests'
    start: date = date(2026, 1, 1)
    end: date = date(2026, 12, 31)
    category: str | None = Field(default=None, max_length=200)
    status: str | None = Field(default=None, max_length=100)
    community: str | None = Field(default=None, max_length=200)
    zipcode: str | None = Field(default=None, max_length=20)
    exclude_linked: bool = False
    # Derived classification (category_mapping.csv). category stays the original
    # source service_name; '__missing__' selects records not yet classified.
    main_category: str | None = Field(default=None, max_length=200)
    sub_category: str | None = Field(default=None, max_length=200)

    @model_validator(mode='after')
    def valid_dates(self):
        if self.start > self.end or self.start.year != 2026 or self.end.year != 2026:
            raise ValueError('Choose an increasing date range within 2026.')
        if self.dataset == 'traffic_collisions' and (self.community or self.zipcode or self.exclude_linked
                                                    or self.main_category or self.sub_category):
            raise ValueError('Planning area, ZIP, linked-request and category-hierarchy filters apply only to service requests.')
        return self


class ViewportContext(MapModel):
    bounds: Bounds
    filters: ExplorerFilters = Field(default_factory=ExplorerFilters)


class MapCircle(MapModel):
    location_id: str
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    radius_meters: float = Field(ge=0, le=50000)
    datasets: list[str]
    evidence_ids: list[str]


class MapVisualization(MapModel):
    mode: Literal['radius', 'viewport']
    circles: list[MapCircle] = Field(default_factory=list)
    bounds: Bounds | None = None
    datasets: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
