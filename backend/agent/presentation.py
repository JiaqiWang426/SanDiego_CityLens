"""Human-readable views derived only from verified evidence, never model prose."""
from datetime import timedelta
from zoneinfo import ZoneInfo
from .models import Model, TimeRange
from pydantic import Field


class MetricCard(Model):
    location_id: str
    label: str
    dataset: str
    value: int | None
    available: bool
    unit: str='reports'
    evidence_ids: list[str]
    metric_kind: str


class AnalysisPresentation(Model):
    summary: str
    analysis_type: str
    title: str
    interpretation: str
    locations: list[dict]
    metrics: list[MetricCard]
    time_range: TimeRange | None
    timeframe_label: str | None
    spatial_scope: dict
    data_coverage: list[dict]
    limitations: list[str]


def build_presentation(response,request):
    valid={e.evidence_id:e for e in response.evidence if e.status in {'ok','partial'} and e.value is not None}
    supported=response.status in {'ok','partial'}
    metrics=[]
    labels={loc.id:loc.label or 'Location '+loc.id for loc in request.locations}
    for e in response.evidence:
        # Retrieval page sizes are explicitly separate from total counts.
        available=supported and e.evidence_id in valid
        metrics.append(MetricCard(location_id=e.location_id,label=labels.get(e.location_id,e.location_id),dataset=e.domain,
            value=e.value if available else None,available=available,evidence_ids=[e.evidence_id],
            metric_kind='example_page_size' if e.metric=='retrieved_collision_page_size' else 'record_count'))
    summaries=[]
    if supported and response.comparison:
        for item in response.comparison.metrics:
            if not all(key in valid for key in item.evidence_ids):continue
            domain=valid[item.evidence_ids[0]].domain
            noun='traffic collision reports' if domain=='traffic_collisions' else 'service requests'
            difference=item.difference_b_minus_a
            summaries.append(f'Both locations have the same number of recorded {noun}.' if difference==0 else
                f'Location {item.location_b if difference>0 else item.location_a} has {abs(difference):,} more recorded {noun} in the selected radius.')
    if supported and not summaries:
        counts=[m for m in metrics if m.available and m.metric_kind=='record_count']
        summaries=[f'{m.value:,} recorded {"traffic collision reports" if m.dataset=="traffic_collisions" else "service requests"} near Location {m.location_id}.' for m in counts]
        if not summaries and any(m.available for m in metrics):summaries=['The requested source-report examples are ready. The page size is not a total count.']
    if not supported or not summaries:
        summaries=['This analysis could not establish a verified result.' if response.status=='error' else 'This request needs clarification or data beyond the supported sources.']
    tz=ZoneInfo(request.time_range.calendar_timezone)
    start=request.time_range.start.astimezone(tz)
    end=request.time_range.end.astimezone(tz)
    # Display the inclusive final date only for intervals ending at midnight.
    through=end-timedelta(days=1) if end.hour==end.minute==end.second==end.microsecond==0 else end
    timeframe=f'{start:%b %d, %Y} – {through:%b %d, %Y}'
    coverage=[]
    for domain in dict.fromkeys(e.domain for e in response.evidence):
        evidence=next(e for e in response.evidence if e.domain==domain)
        q=evidence.quality
        coverage.append(dict(dataset=domain,observed_start=q.dataset_observed_start,
            observed_end=q.dataset_observed_end,dataset_records=q.dataset_records,
            located_records=q.dataset_located_records,coverage_scope='dataset_wide_not_radius_coverage',
            source_freshness_verified=evidence.provenance.source_freshness_verified,
            requested_interval_preserved=True))
    return AnalysisPresentation(summary=' '.join(summaries),analysis_type=response.intent,
        title='Location comparison' if response.comparison else 'Location investigation',
        interpretation='These are recorded reports within the requested scope. Counts alone do not establish safety, risk, or completed repairs.',
        locations=[dict(id=loc.id,label=labels[loc.id]) for loc in request.locations],metrics=metrics,
        time_range=request.time_range,timeframe_label=timeframe,
        spatial_scope=dict(mode='radius',radius_meters=request.radius_meters),data_coverage=coverage,
        limitations=list(dict.fromkeys(response.limitations)))
