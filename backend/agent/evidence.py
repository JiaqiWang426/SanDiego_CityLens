"""Evidence construction, calendar normalization, comparison and claim checking."""
from __future__ import annotations

from datetime import datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

from pyproj import Geod

from .models import (Bucket, Claim, ComparisonMetric, EvidenceRecord, Provenance,
                     Quality, Scope)
from .tools import REGISTRY, ToolResult

COMMON_CAVEATS = [
    'Geographic completeness and source freshness are not verified.',
    'Import timestamps do not establish source freshness; upserts retain older records.',
    'Source wall timestamps are assumed to be America/Los_Angeles; the source timezone is unverified.',
]
DOMAIN_CAVEATS = {
    'service_requests': [
        'Imported Get It Done subset (including CD1 where present); citywide completeness is unknown.',
        'Counts measure request IDs, not distinct physical issues or public service quality.',
        'Reporting bias and missing coordinates can affect these counts; Closed does not prove repair.',
    ],
    'traffic_collisions': [
        'Police-recorded reports only; these counts do not measure collision risk, crime, or safety.',
        'Spatial results exclude unlocated reports; their number within the radius cannot be determined.',
        'Collision coordinates are block-approximate; radius membership is uncertain near boundaries.',
    ],
}


def _floor(value: datetime, period: str, tz: ZoneInfo) -> datetime:
    local=value.astimezone(tz).replace(hour=0,minute=0,second=0,microsecond=0)
    if period=='week':
        return local-timedelta(days=local.weekday())
    if period=='month':
        return local.replace(day=1)
    if period=='quarter':
        return local.replace(month=((local.month-1)//3)*3+1,day=1)
    if period=='year':
        return local.replace(month=1,day=1)
    return local


def _next(value: datetime, period: str) -> datetime:
    if period in {'day','week'}:
        return value+timedelta(days=1 if period=='day' else 7)
    months={'month':1,'quarter':3,'year':12}[period]
    index=value.year*12+value.month-1+months
    return value.replace(year=index//12,month=index%12+1)


def normalize_buckets(result: ToolResult) -> list[Bucket]:
    """Fill requested calendar buckets, marking absent SQL rows explicitly.

    Zero-filled buckets mean no matching rows were returned, not verified
    source coverage. Edge buckets retain calendar bounds; scope clips the query.
    """
    call=result.call
    current=_floor(call.time_range.start,call.period,ZoneInfo(call.time_range.calendar_timezone))
    raw={row['period_start']:row for row in result.rows}
    buckets=[]
    while current < call.time_range.end:
        end=_next(current,call.period)
        row=raw.get(current)
        buckets.append(Bucket(period_start=current,period_end=end,
            report_count=row['collision_count'] if row else 0,
            located_count=row['located_count'] if row else 0,
            unknown_injury_count=row['unknown_injured_count'] if row else 0,
            unknown_fatality_count=row['unknown_killed_count'] if row else 0,
            source_bucket_present=row is not None))
        current=end
        if len(buckets)>3662:
            raise ValueError('Calendar normalization budget exceeded')
    # Detect malformed/out-of-window rows rather than dropping their evidence.
    if len(raw)!=len(result.rows) or sum(b.source_bucket_present for b in buckets)!=len(raw):
        raise ValueError('Unexpected aggregation bucket boundaries')
    return buckets


def make_evidence(result: ToolResult) -> EvidenceRecord:
    """Only executed tool results can populate values and IDs."""
    call=result.call
    domain,table,dataset,url=REGISTRY[call.name]
    scope=Scope(latitude=call.location.latitude,longitude=call.location.longitude,
        radius_meters=call.radius_meters,start_inclusive=call.time_range.start,
        end_exclusive=call.time_range.end,calendar_timezone=call.time_range.calendar_timezone)
    filters=({'exclude_linked_duplicates':call.exclude_linked_duplicates} if domain=='service_requests'
             else call.collision_filters.model_dump())
    if call.name=='aggregate_traffic_collisions':
        filters['period']=call.period
    caveats=COMMON_CAVEATS+DOMAIN_CAVEATS[domain]
    imports=result.metadata.get('dataset_imports',[])
    provenance=Provenance(dataset_import_ids=[row['id'] for row in imports],
        dataset_imports=imports,dataset_imports_truncated=result.metadata.get('dataset_imports_truncated',False))
    quality=Quality(**{k:v for k,v in result.metadata.items() if k in Quality.model_fields},
        coordinate_precision='block_approximate' if domain=='traffic_collisions' else 'source_coordinates_unverified')
    values=dict(value=None,buckets=[],records=[],pagination=None)
    if call.name=='count_service_requests':
        metric='request_id_count'
        definition='Distinct stored request IDs created in the selected radius and half-open interval.'
        if result.status=='ok':
            values['value']=result.rows[0]['request_id_count']
    elif call.name=='aggregate_traffic_collisions':
        metric='geocoded_collision_report_count'
        definition='Stored geocoded police-collision report IDs in the selected radius and half-open interval.'
        if result.status=='ok':
            buckets=normalize_buckets(result)
            values.update(value=sum(b.report_count for b in buckets),buckets=buckets)
            caveats=caveats+['Missing calendar buckets are zero-filled matches, not verified data coverage.']
    else:
        metric='retrieved_collision_page_size'
        definition='Number of collision reports in this bounded retrieval page, not the total matches.'
        if result.status=='ok':
            # The API may carry untrusted street text for human inspection.
            # None of these record fields is forwarded to either LLM call;
            # the composer receives only verified claim IDs, metrics and values.
            safe_fields=('report_id','occurred_at','injured','killed','hit_and_run',
                         'hit_run_level','primary_street','intersecting_street',
                         'normalized_address','geocode_status','geocode_precision','import_id')
            records=[{key:row.get(key) for key in safe_fields} for row in result.rows]
            values.update(value=len(records),records=records,pagination={
                'limit':call.limit,'offset':0,'total_matches':None,
                'may_have_more':len(records)==call.limit})
    return EvidenceRecord(evidence_id='ev_'+uuid4().hex,query_id=result.query_id,
        domain=domain,source_table=table,source_dataset=dataset,source_url=url,
        tool=call.name,metric=metric,metric_definition=definition,location_id=call.location.id,
        scope=scope,filters=filters,provenance=provenance,quality=quality,
        status='partial' if result.status=='ok' else result.status,
        executed_at=result.executed_at,elapsed_ms=result.elapsed_ms,caveats=caveats,
        error_code=result.error_code,**values)


def compare_pair(a: EvidenceRecord, b: EvidenceRecord) -> ComparisonMetric:
    """Compare observed counts only after exact scope/definition compatibility."""
    if (a.value is None or b.value is None or a.location_id==b.location_id
            or a.metric=='retrieved_collision_page_size' or a.metric!=b.metric
            or a.metric_definition!=b.metric_definition or a.unit!=b.unit or a.filters!=b.filters
            or a.scope.model_dump(exclude={'latitude','longitude'})!=b.scope.model_dump(exclude={'latitude','longitude'})):
        raise ValueError('Evidence is not comparable')
    _,_,distance=Geod(ellps='WGS84').inv(a.scope.longitude,a.scope.latitude,b.scope.longitude,b.scope.latitude)
    overlap=distance<=a.scope.radius_meters+b.scope.radius_meters
    limits=list(dict.fromkeys(a.caveats+b.caveats))
    if overlap:
        limits.append('Query circles overlap; counts may share records and are not independent samples.')
    return ComparisonMetric(metric=a.metric,location_a=a.location_id,location_b=b.location_id,
        value_a=a.value,value_b=b.value,difference_b_minus_a=b.value-a.value,
        evidence_ids=[a.evidence_id,b.evidence_id],overlapping_circles=overlap,limitations=limits)


def record_claim(e: EvidenceRecord) -> Claim:
    if e.value is None:
        raise ValueError('Cannot construct a count claim from failed/unavailable evidence')
    page=e.metric=='retrieved_collision_page_size'
    noun={'request_id_count':'imported Get It Done request IDs',
          'geocoded_collision_report_count':'imported, geocoded police collision reports',
          'retrieved_collision_page_size':'reports in the retrieved page (not total matches)'}[e.metric]
    text=f'{e.location_id}: {e.value} {noun} matched the recorded query filters [{e.evidence_id}].'
    return Claim(claim_id='cl_'+uuid4().hex,text=text,claim_type='page_size' if page else 'record_count',
        evidence_ids=[e.evidence_id],metric=e.metric,value=e.value,limitations=e.caveats)


def comparison_claim(c: ComparisonMetric) -> Claim:
    text=(f'{c.metric}: {c.location_b} minus {c.location_a} is '
          f'{c.difference_b_minus_a:+d} reports [{", ".join(c.evidence_ids)}].')
    return Claim(claim_id='cl_'+uuid4().hex,text=text,claim_type='descriptive_comparison',
        evidence_ids=c.evidence_ids,metric=c.metric,value=c.difference_b_minus_a,limitations=c.limitations)


def verify_claims(claims: list[Claim], evidence: list[EvidenceRecord]) -> None:
    """Recompute exact allowed claims, checking prose as well as numbers/refs."""
    index={e.evidence_id:e for e in evidence}
    if len(index)!=len(evidence) or len({c.claim_id for c in claims})!=len(claims):
        raise ValueError('Duplicate evidence/claim IDs')
    for claim in claims:
        if any(key not in index for key in claim.evidence_ids):
            raise ValueError('Missing evidence reference')
        refs=[index[key] for key in claim.evidence_ids]
        if claim.claim_type=='descriptive_comparison':
            if len(refs)!=2:
                raise ValueError('Comparison requires two references')
            expected=comparison_claim(compare_pair(*refs))
        else:
            if len(refs)!=1:
                raise ValueError('Count claim requires one reference')
            expected=record_claim(refs[0])
        if claim.model_dump(exclude={'claim_id'})!=expected.model_dump(exclude={'claim_id'}):
            raise ValueError('Claim does not match evidence')
