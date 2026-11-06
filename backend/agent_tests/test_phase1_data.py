"""Phase 1.1 independent category reference and isolated DB verification.

No real CSV is changed. Integration uses the existing guarded disposable fixture.
"""
import asyncio
from datetime import datetime
from unittest.mock import MagicMock,patch

from fastapi.testclient import TestClient
import numpy as np
import pandas as pd
from pyproj import Geod
import psycopg
from psycopg.types.json import Jsonb
from pydantic import ValidationError
import pytest

from backend.agent.evidence import make_evidence,record_claim,verify_claims
from backend.agent.models import Plan,ServiceRequestAggregation
from backend.agent.settings import Settings
from backend.agent.tools import DatabaseTools,MetadataCall,ToolCall,ToolResult,query_spec
from backend.agent.workflow import Agent,validated_calls
from backend.agent_tests.helpers import FakeModel,FakeTools,request
from backend.agent_tests.test_integration import isolated_db
from backend.collision_geocoding import GeocodeResult,save_geocode,geocode_pending
from backend.dataset_metadata import DatasetMetadata,get_dataset_metadata
from backend.main import app,get_agent
from backend.service_request_queries import aggregate_service_requests

START='2026-03-08T00:00:00-08:00'
END='2026-03-09T00:00:00-07:00'
LAT,LNG=32.84,-117.25


def sample_rows():
    geod=Geod(ellps='WGS84')
    rows=[]
    cases=[(' Pothole ',' closed ',None,0,START),
        ('pothole','CLOSED','p1',0,'2026-03-08T03:00:00-07:00'),
        ('Pothole','New',None,0,END),
        ('','\t ',None,0,'2026-03-08T04:00:00-07:00'),
        (None,'Unknown',None,0,'2026-03-08T04:00:00-07:00'),
        ('UNKNOWN',None,None,0,'2026-03-08T04:00:00-07:00'),
        ('Parking','Closed',None,0,'2026-03-08T04:00:00-07:00'),
        ('Parking Violation','Closed',None,0,'2026-03-08T04:00:00-07:00'),
        ('Parking - 72-Hours','New',None,None,'2026-03-08T04:00:00-07:00'),
        ('\tPOTHOLE\t','In   Process',None,0,'2026-03-08T04:00:00-07:00'),
        ('Pothole','Closed',None,799.9,'2026-03-08T04:00:00-07:00'),
        ('Pothole','Closed',None,800.1,'2026-03-08T04:00:00-07:00')]
    for i,(service,status,parent,distance,date) in enumerate(cases,1):
        lng,lat,_=geod.fwd(LNG,LAT,90,distance or 0)
        rows.append(dict(id=f'p{i}',service=service,status=status,parent=parent,
            lng=lng if distance is not None else None,lat=lat if distance is not None else None,date=date))
    return rows


def reference(group_by,exclude=False,service_name=None,status=None,radius=800,start=START,end=END):
    """Independent pandas normalization/filter/group calculation, no production helpers."""
    frame=pd.DataFrame(sample_rows())
    for column in ['service','status']:
        frame[column]=frame[column].str.replace(r'\s+',' ',regex=True).str.strip().str.upper().replace('',None)
    dates=pd.to_datetime(frame.date,utc=True,format='mixed')
    mask=(dates>=pd.Timestamp(start)) & (dates<pd.Timestamp(end)) & frame.lat.notna() & frame.lng.notna()
    if exclude:
        mask &= frame.parent.isna()
    for column,value in [('service',service_name),('status',status)]:
        if value is not None:
            canonical=' '.join(value.upper().split()) or None
            mask &= frame[column].isna() if canonical is None else frame[column].eq(canonical)
    frame=frame[mask].copy()
    if frame.empty:
        return {}
    _,_,dist=Geod(ellps='WGS84').inv(np.full(len(frame),LNG),np.full(len(frame),LAT),frame.lng.to_numpy(),frame.lat.to_numpy())
    frame=frame[np.asarray(dist)<=radius].copy()
    if group_by=='service_name': frame['status']=None
    if group_by=='status': frame['service']=None
    counts=frame.groupby(['service','status'],dropna=False)['id'].nunique()
    return {tuple(None if pd.isna(x) else x for x in key):int(value) for key,value in counts.items()}


# A service-only plan must be paired with a service question: the validator now
# rejects plans that omit a dimension the question names (e.g. collisions).
SERVICE_QUESTION='How many Get It Done requests were submitted?'


def test_new_tool_parameter_validation_and_sql_binding():
    with pytest.raises(ValidationError): ServiceRequestAggregation(group_by='free_sql')
    with pytest.raises(ValidationError): MetadataCall(domain='crime')
    call=validated_calls(request(question=SERVICE_QUESTION),Plan(intent='request_count',tools=['aggregate_service_requests']),Settings())[0]
    statement,params=query_spec(call)
    assert statement.startswith('SELECT * FROM citylens.aggregate_service_requests(')
    assert len(params)==9 and params[6]=='service_name'
    assert query_spec(MetadataCall(domain='traffic_collisions'))[1]==('traffic_collisions',)


def test_group_evidence_and_claim_attribution_are_preserved():
    call=validated_calls(request(question=SERVICE_QUESTION),Plan(intent='request_count',tools=['aggregate_service_requests'],
        service_request_aggregation=ServiceRequestAggregation(group_by='both')),Settings())[0]
    result=ToolResult(call,rows=[dict(service_name=None,status='NEW',request_id_count=2),
                               dict(service_name='POTHOLE',status='CLOSED',request_id_count=3)])
    evidence=make_evidence(result)
    assert evidence.value==5 and evidence.groups[0].service_name is None
    assert evidence.source_table=='citylens.service_requests' and evidence.query_id==result.query_id
    assert evidence.filters['group_by']=='both' and evidence.filters['exclude_linked_duplicates'] is False
    verify_claims([record_claim(evidence)],[evidence])


def test_related_planning_routes_to_grouped_service_tool():
    req=request(question='Group Get It Done reports by service name and status.')
    plan=Plan(intent='request_count',tools=['aggregate_service_requests'],
              service_request_aggregation=ServiceRequestAggregation(group_by='both'))
    class Groups(FakeTools):
        def execute(self,call):
            return ToolResult(call,rows=[dict(service_name='POTHOLE',status='NEW',request_id_count=2)])
    response=asyncio.run(Agent(FakeModel(plan),Groups(),Settings()).query(req))
    assert response.status=='partial' and response.evidence[0].groups[0].request_id_count==2
    verify_claims(response.claims,response.evidence)


def test_specific_category_requires_explicit_service_tool_and_filter():
    req=request(question='How many Pothole reports were submitted?')
    with pytest.raises(ValueError):
        validated_calls(req,Plan(intent='location_summary',tools=['aggregate_traffic_collisions']),Settings())
    with pytest.raises(ValueError):
        validated_calls(req,Plan(intent='request_count',tools=['aggregate_service_requests']),Settings())
    plan=Plan(intent='request_count',tools=['aggregate_service_requests'],filter_quote='Pothole',
        service_request_aggregation=ServiceRequestAggregation(service_name='Pothole'))
    calls=validated_calls(req,plan,Settings())
    assert calls[0].name=='aggregate_service_requests'
    assert calls[0].service_request_aggregation.service_name=='Pothole'


def test_unmatched_precision_and_worker_arguments_are_rejected_without_network():
    conn=MagicMock()
    with pytest.raises(ValueError):
        save_geocode(conn,'id','key',GeocodeResult('no_match','fixture',precision='block_approximate'))
    conn.execute.assert_not_called()
    with pytest.raises(ValueError): geocode_pending(conn,MagicMock(),pause=float('nan'))


def test_worker_requests_pending_status_compare_and_set_without_network():
    conn=MagicMock()
    conn.execute.return_value.fetchall.return_value=[('id','key',{})]
    provider=MagicMock()
    provider.geocode.return_value=GeocodeResult('no_match','synthetic')
    with patch('backend.collision_geocoding.save_geocode',return_value=False) as save, \
         patch('backend.collision_geocoding.time.sleep'):
        stats=geocode_pending(conn,provider,pause=0)
    assert save.call_args.kwargs=={'only_if_pending':True}
    assert stats=={'source_or_status_changed':1}


@pytest.fixture(scope='module')
def reliability_db(isolated_db):
    admin_url,reader_url=isolated_db
    with psycopg.connect(admin_url) as conn:
        run=conn.execute("""INSERT INTO citylens.ingestions(source_file,sha256,source_rows,distinct_requests)
            VALUES ('phase1_synthetic.csv','synthetic',12,12) RETURNING id""").fetchone()[0]
        for row in sample_rows():
            conn.execute('''INSERT INTO citylens.service_requests
                (service_request_id,parent_id,requested_at,service_name,status,location,raw_record,ingestion_id)
                VALUES (%s,%s,%s,%s,%s,CASE WHEN %s::float8 IS NOT NULL THEN
                    ST_SetSRID(ST_MakePoint(%s,%s),4326)::geography END,%s,%s)''',
                (row['id'],row['parent'],row['date'],row['service'],row['status'],row['lat'],
                 row['lng'],row['lat'],Jsonb(row),run))
        run=conn.execute("INSERT INTO citylens.collision_imports(source_file,sha256) VALUES ('phase1.csv','synthetic') RETURNING id").fetchone()[0]
        for status in ['pending','no_match','ambiguous','unsupported','error']:
            conn.execute('''INSERT INTO citylens.traffic_collisions
                (report_id,primary_street,intersecting_street,normalized_address,address_key,
                 raw_record,validation_issues,import_id,geocode_status)
                VALUES (%s,'TEST ST','','TEST ST','key','{}','[]',%s,%s)''',('phase1_'+status,run,status))
    return admin_url,reader_url


@pytest.mark.parametrize('group_by',['service_name','status','both'])
@pytest.mark.parametrize('exclude',[False,True])
def test_group_totals_match_independent_pandas_and_existing_counter(reliability_db,group_by,exclude):
    _,reader_url=reliability_db
    with psycopg.connect(reader_url) as conn:
        rows=aggregate_service_requests(conn,LAT,LNG,800,START,END,group_by=group_by,exclude_linked_duplicates=exclude)
        actual={(row['service_name'],row['status']):row['request_id_count'] for row in rows}
        assert actual==reference(group_by,exclude)
        total=conn.execute('SELECT citylens.count_service_requests(%s,%s,%s,%s,%s,%s)',(LAT,LNG,800,START,END,exclude)).fetchone()[0]
        assert sum(actual.values())==total
        keys=list(actual)
        assert keys==sorted(keys,key=lambda pair:tuple((v is not None,v or '') for v in pair))


@pytest.mark.parametrize('filters',[dict(service_name=' pothole ',status=' closed '),
    dict(service_name=''),dict(status=''),dict(service_name='Unknown'),dict(service_name='Parking'),
    dict(service_name='not present')])
def test_filters_unknowns_empty_and_semantic_categories(reliability_db,filters):
    _,reader=reliability_db
    with psycopg.connect(reader) as conn:
        rows=aggregate_service_requests(conn,LAT,LNG,800,START,END,group_by='both',**filters)
        assert {(r['service_name'],r['status']):r['request_id_count'] for r in rows}==reference('both',**filters)


@pytest.mark.parametrize('radius',[0,799,800,801])
def test_spatial_boundary_behavior_matches_geodesic_reference(reliability_db,radius):
    _,reader=reliability_db
    with psycopg.connect(reader) as conn:
        rows=aggregate_service_requests(conn,LAT,LNG,radius,START,END,group_by='both')
    assert {(r['service_name'],r['status']):r['request_id_count'] for r in rows}==reference('both',radius=radius)


def test_timezone_start_end_and_dst_boundary(reliability_db):
    _,reader=reliability_db
    start,end='2026-03-08T00:00:00-08:00','2026-03-08T03:00:00-07:00'
    with psycopg.connect(reader) as conn:
        rows=aggregate_service_requests(conn,LAT,LNG,800,start,end,group_by='both')
    assert {(r['service_name'],r['status']):r['request_id_count'] for r in rows}==reference('both',start=start,end=end)
    assert sum(r['request_id_count'] for r in rows)==1


@pytest.mark.parametrize('change',[dict(group_by='sql'),dict(latitude=91),dict(radius_meters=-1),
    dict(end_time=START),dict(exclude_linked_duplicates=None)])
def test_invalid_sql_parameters(reliability_db,change):
    _,reader=reliability_db
    params=dict(latitude=LAT,longitude=LNG,radius_meters=800,start_time=START,end_time=END)
    params.update(change)
    with psycopg.connect(reader) as conn,pytest.raises(psycopg.errors.InvalidParameterValue):
        aggregate_service_requests(conn,**params)


@pytest.mark.parametrize('domain',['service_requests','traffic_collisions'])
def test_dataset_metadata_counts_statuses_and_unknown_source_freshness(reliability_db,domain):
    _,reader=reliability_db
    with psycopg.connect(reader) as conn:
        data=get_dataset_metadata(conn,domain)
    assert data.dataset_located_records+data.dataset_unlocated_records==data.dataset_records
    assert data.last_successful_local_ingestion_at is not None
    assert data.source_updated_at is None and data.source_publication_at is None
    assert data.source_freshness_verified is False and data.coverage_verified is False
    if domain=='traffic_collisions':
        assert set(data.geocode_status_breakdown)=={'pending','matched','no_match','ambiguous','unsupported','error'}
        assert sum(data.geocode_status_breakdown.values())==data.dataset_records
        assert data.geocoding_coverage==pytest.approx(data.dataset_located_records/data.dataset_records)
    else:
        assert data.geocoding_coverage is None and data.geocode_status_breakdown is None


def test_metadata_missing_history_and_successful_empty_import(reliability_db):
    admin,_=reliability_db
    with psycopg.connect(admin) as conn:
        # Disposable test data only; rollback restores the shared synthetic set.
        conn.execute('DELETE FROM citylens.service_requests')
        conn.execute('DELETE FROM citylens.ingestions')
        row=conn.execute("SELECT citylens.get_dataset_metadata('service_requests')").fetchone()[0]
        data=DatasetMetadata.model_validate(row)
        assert data.dataset_records==0 and data.last_successful_local_ingestion_at is None
        assert data.dataset_observed_start is None and data.coordinate_coverage is None
        conn.execute("""INSERT INTO citylens.ingestions(source_file,sha256,source_rows,distinct_requests)
            VALUES ('empty.csv','synthetic',0,0)""")
        row=conn.execute("SELECT citylens.get_dataset_metadata('service_requests')").fetchone()[0]
        assert row['last_successful_local_ingestion_at'] is not None
        assert row['dataset_observed_end'] is None and row['source_updated_at'] is None
        conn.rollback()


def test_metadata_agent_tool_and_read_only_permissions(reliability_db):
    _,reader=reliability_db
    result=DatabaseTools(reader).execute(MetadataCall(domain='traffic_collisions'))
    assert result.status=='ok' and result.rows[0]['metadata']['domain']=='traffic_collisions'
    model=FakeModel()
    agent=Agent(model,DatabaseTools(reader),Settings())
    app.dependency_overrides[get_agent]=lambda:agent
    try:
        with TestClient(app) as client:
            response=client.post('/api/v1/tools/run',json={'name':'get_dataset_metadata','domain':'service_requests'})
        assert response.status_code==200 and response.json()['metadata']['dataset_records']>0
        assert response.json()['tool_call']['query_id'] and model.plans==0
    finally:
        app.dependency_overrides.clear()
    with psycopg.connect(reader) as conn,pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute("UPDATE citylens.service_requests SET status='altered'")


def test_pending_compare_and_set_does_not_overwrite_completed_match(reliability_db):
    admin,_=reliability_db
    with psycopg.connect(admin) as conn:
        matched=GeocodeResult('matched','fixture',LNG,LAT,'block_approximate')
        assert save_geocode(conn,'phase1_pending','key',matched,only_if_pending=True)
        assert not save_geocode(conn,'phase1_pending','key',GeocodeResult('error','slow-fixture'),only_if_pending=True)
        assert conn.execute("SELECT geocode_status FROM citylens.traffic_collisions WHERE report_id='phase1_pending'").fetchone()[0]=='matched'
        conn.rollback()


def test_query_time_normalization_preserves_original_values(reliability_db):
    _,reader=reliability_db
    with psycopg.connect(reader) as conn:
        aggregate_service_requests(conn,LAT,LNG,800,START,END)
        row=conn.execute("SELECT service_name,status,raw_record FROM citylens.service_requests WHERE service_request_id='p1'").fetchone()
        assert row[0]==' Pothole ' and row[1]==' closed '
        assert row[2]['service']==' Pothole ' and row[2]['status']==' closed '


def test_metadata_rejects_invalid_domain_and_functions_do_not_elevate_privileges(reliability_db):
    _,reader=reliability_db
    with psycopg.connect(reader) as conn:
        names=conn.execute("""SELECT p.proname,p.prosecdef,p.provolatile FROM pg_proc p
            JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='citylens'
            AND p.proname IN ('get_dataset_metadata','aggregate_service_requests')""").fetchall()
        assert sorted(row[0] for row in names)==['aggregate_service_requests','aggregate_service_requests','get_dataset_metadata']
        assert all(not row[1] and row[2]=='s' for row in names)
        with pytest.raises(psycopg.errors.InvalidParameterValue):
            conn.execute("SELECT citylens.get_dataset_metadata('unknown')")


def test_empty_points_are_not_usable_coordinates(reliability_db):
    admin,_=reliability_db
    with psycopg.connect(admin) as conn:
        before=conn.execute("SELECT citylens.get_dataset_metadata('traffic_collisions')").fetchone()[0]
        conn.execute("UPDATE citylens.traffic_collisions SET location='SRID=4326;POINT EMPTY'::geography WHERE report_id='a1'")
        after=conn.execute("SELECT citylens.get_dataset_metadata('traffic_collisions')").fetchone()[0]
        assert after['dataset_located_records']==before['dataset_located_records']-1
        assert after['dataset_unlocated_records']==before['dataset_unlocated_records']+1
        assert after['geocode_status_breakdown']==before['geocode_status_breakdown']
        conn.rollback()
