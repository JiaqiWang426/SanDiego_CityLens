"""Stage B guards and an opt-in disposable PostGIS end-to-end recovery test."""
from contextlib import contextmanager
from copy import deepcopy
import gzip
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import psycopg
import pytest
from pyproj import Transformer

from backend.agent_tests.test_integration import isolated_db
from backend.agent_tests.test_intersection_feasibility import ordinary
from backend.collision_addresses import normalize_collision_address
from backend.sangis_topology import acquire as acquisition, batch, database, apply as deployment
from backend.sangis_topology.acquire import EvidenceError, digest, save, sha
from backend.sangis_topology.verify import select


def road_row():
    raw = {'ADDRESS_NO_PRIMARY':'100','ADDRESS_ROAD_PRIMARY':'A','ADDRESS_SFX_PRIMARY':'ST',
           'ADDRESS_NAME_INTERSECTING':'B','ADDRESS_SFX_INTERSECTING':'ST'}
    address = normalize_collision_address(raw)
    return {'report_id':'r1','raw_record':raw,'address_key':address.address_key,'primary_street':address.primary_street,
            'intersecting_street':address.intersecting_street,'police_beat':'123','geocode_status':'pending','has_location':False}


@pytest.mark.parametrize('change',[{'count':2,'objectIds':[1]}, {'count':2,'objectIds':[1,1]},
                                  {'count':1,'objectIds':['1']}, {'count':1,'objectIds':[True]}])
def test_complete_unique_integer_inventory_is_mandatory(change):
    with pytest.raises(EvidenceError):
        acquisition.checked_ids({'objectIds':change['objectIds']},change['count'])


@pytest.mark.parametrize('payload',[None,[],{'error':{'code':500}},{'exceededTransferLimit':True}])
def test_truncated_or_failed_extractions_stop(payload):
    with pytest.raises(EvidenceError):
        acquisition.validate_response(payload)


def test_missing_batch_or_schema_drift_never_becomes_no_match():
    meta = {'fields':[{'name':'objectid'},{'name':'roadsegid'}],'objectIdField':'objectid'}
    payload = {'spatialReference':{'wkid':2230},'features':[{'attributes':{'objectid':1,'roadsegid':42},'geometry':None}]}
    assert len(acquisition.validate_batch(payload,[1],meta))==1
    for expected,changed in [([1,2],payload),([2],payload),([1],dict(payload,spatialReference={'wkid':4326}))]:
        with pytest.raises(EvidenceError):
            acquisition.validate_batch(changed,expected,meta)
    changed = deepcopy(payload)
    del changed['features'][0]['attributes']['roadsegid']
    with pytest.raises(EvidenceError):
        acquisition.validate_batch(changed,[1],meta)


def test_request_budget_resume_and_offline_replay(tmp_path):
    capture = acquisition.Capture(tmp_path,cap=1,live=True)
    response = MagicMock()
    response.__enter__.return_value.read.return_value = b'{"count":2}'
    opener = MagicMock()
    opener.open.return_value = response
    with patch.object(acquisition,'build_opener',return_value=opener):
        assert capture.query('https://example.test',{'f':'json'},'count')['count']==2
    offline = acquisition.Capture(tmp_path,cap=1)
    with patch.object(acquisition,'build_opener') as http:
        assert offline.query('https://example.test',{'f':'json'},'count')['count']==2
        with pytest.raises(EvidenceError):
            offline.query('https://example.test',{'f':'json'},'missing')
    http.assert_not_called()
    offline.live=True
    with pytest.raises(EvidenceError,match='budget'):
        offline.query('https://example.test',{'f':'json'},'count',fresh=True)
    target=next((tmp_path/'cache').glob('*.gz'))
    with gzip.open(target,'rt',encoding='utf-8') as stream:
        record=json.load(stream)
    record['response']['count']=999
    with gzip.open(target,'wt',encoding='utf-8') as stream:
        json.dump(record,stream)
    with pytest.raises(EvidenceError,match='checksum'):
        offline.query('https://example.test',{'f':'json'},'count')


def test_original_z_geometry_is_preserved_and_not_interpreted_as_level():
    feature={'geometry':{'paths':[[[1,2,0],[3,4,0]]]}}
    assert json.loads(database.geojson(feature,'Roads_All'))['coordinates'][0][0]==[1,2,0]
    assert json.loads(database.geojson({'geometry':{'x':1,'y':2,'z':0}},'Roads_Intersection'))['coordinates']==[1,2,0]


@pytest.mark.parametrize('offset,classification',[(1.99,'accepted_candidate'),(2.01,'topology_uncertain')])
def test_stage_a_alignment_ceiling_is_measured_not_a_snap(offset,classification):
    roads,nodes,incidence=ordinary()
    forward=Transformer.from_crs(4326,32611,always_xy=True)
    reverse=Transformer.from_crs(32611,4326,always_xy=True)
    point=forward.transform(-117.15,32.8)
    x,y=reverse.transform(point[0]+offset,point[1])
    nodes[1][0]['geometry']={'x':x,'y':y}
    assert batch.stage_a.classify_pair('A ST','B ST',roads,nodes,incidence)['status']==classification


def test_source_fingerprint_detects_whitespace_and_unknown_qualifiers():
    row=road_row()
    for key,value in [('ADDRESS_NO_PRIMARY','200'),('ADDRESS_ROAD_PRIMARY',' A '),('ADDRESS_LOCATION_QUALIFIER','BEHIND')]:
        changed=deepcopy(row)
        changed['raw_record'][key]=value
        assert batch.source_fingerprint(changed)!=batch.source_fingerprint(row)


def test_source_keys_retain_block_direction_and_area_qualifiers():
    row=road_row()
    for field,value in [('ADDRESS_NO_PRIMARY','200'),('ADDRESS_OFFSET','100 FT N')]:
        changed=deepcopy(row)
        changed['raw_record'][field]=value
        assert batch.source_key(changed)!=batch.source_key(row)
    changed=deepcopy(row)
    changed['primary_street']='N A ST'
    assert batch.source_key(changed)!=batch.source_key(row)
    changed=deepcopy(row)
    changed['police_beat']='999'
    assert batch.source_key(changed)!=batch.source_key(row)


def test_apply_refuses_changed_sources_existing_coordinates_and_other_statuses():
    row=road_row()
    candidate={'source_address_fingerprint':batch.source_fingerprint(row)}
    assert deployment.eligibility(row,candidate) is None
    for field,value in [('geocode_status','matched'),('geocode_status','ambiguous'),('has_location',True)]:
        assert deployment.eligibility(dict(row,**{field:value}),candidate)=='not_pending_or_already_located'
    changed=deepcopy(row)
    changed['raw_record']['ADDRESS_NO_PRIMARY']='200'
    assert deployment.eligibility(changed,candidate)=='stale_source_address'
    assert deployment.eligibility(None,candidate)=='missing_record'


def test_default_deployment_preflight_cannot_open_a_maintenance_connection(tmp_path):
    row=road_row()
    proposed={'r1':{'source_address_fingerprint':batch.source_fingerprint(row)}}
    conn=MagicMock()
    conn.execute.return_value.fetchall.return_value=[row]
    @contextmanager
    def reader():
        yield conn
    with patch.object(deployment,'validate_approval',return_value=({'candidate_sha256':'x'},proposed,{'allowlist':proposed,'manifest_sha256':'x'})), \
         patch.object(deployment,'connection',reader),patch.object(deployment,'sha',return_value='x'), \
         patch.object(deployment,'maintenance_connection') as maintenance:
        result=deployment.deploy(tmp_path,tmp_path/'approval.json',stage_c=tmp_path)
    maintenance.assert_not_called()
    assert result['collision_updates']==0 and result['counts']=={'eligible':1}


def test_missing_approval_fails_before_any_database_write(tmp_path):
    with patch.object(deployment,'maintenance_connection') as maintenance:
        with pytest.raises(FileNotFoundError):
            deployment.deploy(tmp_path,tmp_path/'no-approval.json',apply=True)
    maintenance.assert_not_called()


def test_independent_sample_includes_high_risk_ambiguity_and_grade_separation():
    attrs={i:{'segclass':'1' if i%2 else '5'} for i in range(1,1000)}
    results=[]
    for index in range(70):
        r=batch.stage_a.classify_pair('A ST','B ST',*ordinary(index+1))
        r['pair_id']=str(index)
        if index==0:r['status']='multiple_candidates'
        if index==1:r['status']='no_intersection';r['grade_separated_conflicts']=1
        results.append(r)
    chosen=select(results,attrs)
    assert results[0] in chosen and results[1] in chosen
    assert sum(r['status']=='accepted_candidate' for r in chosen)==60
    assert [r['pair_id'] for r in chosen]==[r['pair_id'] for r in select(list(reversed(results)),attrs)]


def synthetic_snapshot(directory,extra_rejected=False,extra_roads=()):
    roads,nodes,_=ordinary()
    roads+=[deepcopy(r) for r in extra_roads]
    if extra_rejected:
        more,_,_=ordinary(node=2,x=-117.14,type='P')
        for feature in more:
            if feature['attributes']['rd30full']=='B ST':
                feature['attributes'].update(f_level=2,t_level=2)
        roads+=more
    transform=Transformer.from_crs(4326,2230,always_xy=True)
    node_points={1:(-117.15,32.8)}
    for feature in roads:
        a=feature['attributes'];points=feature['geometry']['paths'][0]
        node_points[a['fnode']]=points[0];node_points[a['tnode']]=points[-1]
        feature['geometry']['paths']=[[[*transform.transform(*p),0] for p in points]]
    node_features=[{'attributes':{'objectid':1000+n,'interid':n,'type':'I' if n==1 else 'P' if n==2 else 'E','globalid':'{synthetic-node-%d}'%n},
                    'geometry':dict(zip(('x','y','z'),[*transform.transform(*p),0]))} for n,p in node_points.items()]
    manifest={'complete':True,'layers':{},'files':{},'format_version':1}
    for layer,features in [('Roads_All',roads),('Roads_Intersection',node_features)]:
        meta={'objectIdField':'objectid'}
        save(directory/(layer+'-metadata.json'),meta)
        filename=layer+'.json.gz'
        with gzip.open(directory/filename,'wt',encoding='utf-8') as stream:
            json.dump({'response':{'features':features}},stream)
        ids=sorted(f['attributes']['objectid'] for f in features)
        manifest['layers'][layer]={'feature_count':len(features),'id_sha256':digest(ids),'batch_files':[filename],'source_editing_info':{'lastEditDate':1}}
    manifest['files']={p.name:sha(p) for p in directory.iterdir() if p.is_file()}
    manifest['snapshot_id']=digest({'layers':manifest['layers'],'files':manifest['files']})
    save(directory/'manifest.json',manifest)
    return manifest


def test_snapshot_checksums_reject_corruption(tmp_path):
    manifest=synthetic_snapshot(tmp_path)
    assert acquisition.verify_files(tmp_path)['snapshot_id']==manifest['snapshot_id']
    (tmp_path/'Roads_All.json.gz').write_bytes(b'corrupted')
    with pytest.raises(EvidenceError):
        acquisition.verify_files(tmp_path)


def insert_pending(admin_url,rows):
    """rows: (report_id, raw_record) pairs inserted as Pending without location."""
    with psycopg.connect(admin_url) as conn:
        cid=conn.execute("INSERT INTO citylens.collision_imports(source_file,sha256) VALUES('stage-b-synthetic','fixture') RETURNING id").fetchone()[0]
        for report_id,raw in rows:
            a=normalize_collision_address(raw)
            conn.execute('''INSERT INTO citylens.traffic_collisions(report_id,occurred_at,police_beat,primary_street,intersecting_street,normalized_address,address_key,
                 raw_record,validation_issues,import_id,geocode_details) VALUES(%s,'2021-05-01T12:00:00-07:00','521',%s,%s,%s,%s,%s,'[]',%s,'{"previous_attempt":{"evidence":"keep"}}')''',
                 (report_id,a.primary_street,a.intersecting_street,a.normalized_address,a.address_key,psycopg.types.json.Jsonb(raw),cid))


def synthetic_stage_b(admin_url,data,output,*,extra_rejected=False,extra_roads=()):
    """Import a synthetic snapshot, run the real Stage B dry run, then add the
    synthetic reviewer-side fixtures that real Stage A/TIGER evidence supplies."""
    manifest=synthetic_snapshot(data,extra_rejected=extra_rejected,extra_roads=extra_roads)
    sid=manifest['snapshot_id']
    assert database.import_snapshot(data)['action']=='imported'
    database.validate_snapshot(sid,output/'topology-integrity.json')
    summary=batch.dry_run(sid,output)
    assert not summary['stage_a_replay_passed']  # Synthetic network is not Stage A's real network.
    save(output/'stage-a-replay.json',{'passed':True,'synthetic_test_fixture':True})
    save(output/'rest-archive-comparison.json',{'passed':True,'synthetic_test_fixture':True})
    run=acquisition.read(output/'run-manifest.json')
    run['files']['stage-a-replay.json']=sha(output/'stage-a-replay.json')
    run['files']['rest-archive-comparison.json']=sha(output/'rest-archive-comparison.json')
    save(output/'run-manifest.json',run)
    save(output/'independent-plan.json',{'synthetic_test_fixture':True})
    save(output/'independent-validation.json',[{'synthetic_test_fixture':True}])
    save(output/'independent-evidence'/'requests.json',{'attempts':[]})
    save(output/'independent-summary.json',{'complete':True,'systematic_error_discovered':False,'synthetic_test_fixture':True,
        'files':{'independent-plan.json':sha(output/'independent-plan.json'),'independent-validation.json':sha(output/'independent-validation.json'),
                 'requests.json':sha(output/'independent-evidence'/'requests.json')}})
    return sid,summary


def stage_b_approval(output,sid,report_ids,**extra):
    return {'decision':'approve_apply','reviewer':'synthetic fixture','approved_at':'2026-11-02',
            'candidate_sha256':sha(output/'candidates.jsonl'),'run_manifest_sha256':sha(output/'run-manifest.json'),
            'independent_summary_sha256':sha(output/'independent-summary.json'),'snapshot_id':sid,'method_version':batch.METHOD_VERSION,
            'acknowledge_temporal_uncertainty':True,'acknowledge_independent_validation_limitations':True,
            'historical_review_basis':'Synthetic topology test only','allow_precision_migration':True,'report_ids':report_ids,**extra}


def test_disposable_postgis_import_dry_run_and_stage_c_binding(isolated_db,tmp_path,monkeypatch):
    """Only owned/explicit *_test DB; fixture rejects development port 54329.
    Guarded apply and recovery are exercised through Stage C in test_stage_c.py."""
    admin_url,reader_url=isolated_db
    monkeypatch.setenv('DATABASE_URL',admin_url)
    monkeypatch.setenv('AGENT_DATABASE_URL',reader_url)
    monkeypatch.setenv('SANGIS_MAINTENANCE_DATABASE_URL',admin_url)
    data=tmp_path/'data';data.mkdir()
    output=tmp_path/'out'
    insert_pending(admin_url,[('stage-b-'+n,road_row()['raw_record']) for n in ('1','2')])
    sid,summary=synthetic_stage_b(admin_url,data,output,extra_rejected=True)
    assert database.import_snapshot(data)['action']=='already_imported'
    integrity=acquisition.read(output/'topology-integrity.json')
    assert integrity['usable_for_conservative_proposals'] and integrity['road_segments']['geometry_z_count']==8
    assert integrity['endpoints']['dangling_references']==0
    # Source OBJECTIDs stay unique while duplicated topology INTERIDs remain
    # visible to the resolver; equal coordinates do not justify deduplication.
    with psycopg.connect(admin_url,row_factory=psycopg.rows.dict_row) as conn:
        conn.execute('''INSERT INTO citylens_ref.road_nodes(snapshot_id,objectid,interid,node_type,attributes,source_geometry,geom,geom_4326)
           SELECT snapshot_id,objectid+100000,interid,node_type,attributes,source_geometry,geom,geom_4326
           FROM citylens_ref.road_nodes WHERE snapshot_id=%s AND interid=1''',(sid,))
        resolver=batch.LocalResolver(conn,sid,{('A ST','B ST')})
        result=resolver.resolve('A ST','B ST')
        assert result['status']=='topology_uncertain'
        assert 'missing_or_duplicate_node_record' in result['candidates'][0]['reasons']
        conn.rollback()
    assert summary['accepted_proposal_reports']==2 and summary['collision_fingerprint_unchanged']
    # A Stage B-only approval can no longer deploy: Stage C binding is mandatory.
    approval_path=tmp_path/'synthetic-approval.json'
    save(approval_path,stage_b_approval(output,sid,['stage-b-1','stage-b-2']))
    with pytest.raises((EvidenceError,FileNotFoundError)):
        deployment.deploy(output,approval_path,stage_c=tmp_path/'no-stage-c')
    with patch.object(deployment,'maintenance_connection') as maintenance:
        with pytest.raises((EvidenceError,FileNotFoundError)):
            deployment.deploy(output,approval_path,apply=True,stage_c=tmp_path/'no-stage-c')
    maintenance.assert_not_called()
    with batch.connection() as conn:
        assert database.reference_reader_guard(conn)=={'schema_write':False,'table_write':False}
        assert batch.audit(conn)['source_fields']==summary['after']['source_fields']
