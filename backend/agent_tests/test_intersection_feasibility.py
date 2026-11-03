"""Stage A fixtures only; never query/update historical records or live providers."""
from copy import deepcopy
import gzip
import json
from unittest.mock import MagicMock,patch
from urllib.error import HTTPError

import pytest

from backend.verification.intersection_feasibility.topology import (
    classify_pair,pair_key,street_matches,projected,canonical)
from backend.verification.intersection_feasibility.study import (
    Capture,EvidenceError,features,groups_sample,independent_selection,road_names_variants,node_evidence,population_summary)


def segment(id,name,fn,tn,points,**changes):
    words=name.split()
    attrs=dict(objectid=id,roadsegid=id,rd30full=name,rd30pred='',rd30name=' '.join(words[:-1]),
        rd30sfx=words[-1],rd30postd='',fnode=fn,tnode=tn,f_level=1,t_level=1,
        segclass='5',funclass='L',segstat='C',oneway='B')
    attrs.update(changes)
    return dict(attributes=attrs,geometry=dict(paths=[points]))


def ordinary(node=1,x=-117.15,y=32.8,type='I',t=False):
    p=[x,y];scale=.001
    roads=[segment(node*10+1,'A ST',node*10+11,node,[[x-scale,y],p]),
        segment(node*10+2,'A ST',node,node*10+12,[p,[x+scale,y]]),
        segment(node*10+3,'B ST',node*10+13,node,[[x,y-scale],p])]
    if not t:roads.append(segment(node*10+4,'B ST',node,node*10+14,[p,[x,y+scale]]))
    nodes={node:[dict(attributes=dict(objectid=node+1000,interid=node,type=type),geometry=dict(x=x,y=y))]}
    return roads,nodes,{node:roads}


def evaluate(data):return classify_pair('A ST','B ST',*data)


@pytest.mark.parametrize('t',[False,True])
def test_four_way_and_t_intersections_require_supported_topology(t):
    result=evaluate(ordinary(t=t))
    assert result['status']=='accepted_candidate'
    c=result['candidates'][0]
    assert c['node_id']==1 and c['node_type']=='I' and c['status']=='supported'
    assert c['endpoint_levels']==[1] and c['max_endpoint_distance_m']==0
    assert len(c['incident_segment_ids'])==(3 if t else 4)


def test_two_distinct_intersections_are_never_selected_arbitrarily():
    a,b=ordinary(),ordinary(2,x=-117.14)
    roads=a[0]+b[0];nodes={**a[1],**b[1]};incident={**a[2],**b[2]}
    result=evaluate((roads,nodes,incident))
    assert result['status']=='multiple_candidates' and len(result['candidates'])==2
    reordered=evaluate((list(reversed(roads)),nodes,incident))
    assert reordered==result


def crossing(level_b=1):
    roads=[segment(1,'A ST',11,12,[[-117.151,32.8],[-117.149,32.8]]),
        segment(2,'B ST',13,14,[[-117.15,32.799],[-117.15,32.801]],f_level=level_b,t_level=level_b)]
    return roads,{},{}


def test_geometric_crossing_without_shared_node_is_uncertain():
    result=evaluate(crossing())
    assert result['status']=='topology_uncertain'
    assert result['geometric_crossings']==1 and not result['shared_node_count']


def test_collinear_overlapping_road_names_without_topology_remain_uncertain():
    roads=[segment(1,'A ST',11,12,[[-117.151,32.8],[-117.149,32.8]]),
        segment(2,'B ST',13,14,[[-117.151,32.8],[-117.149,32.8]])]
    assert evaluate((roads,{},{}))['status']=='topology_uncertain'


def test_degenerate_road_geometry_cannot_produce_an_accepted_candidate():
    data=ordinary();data[0][0]['geometry']['paths']=[[[-117.15,32.8],[-117.15,32.8]]]
    assert evaluate(data)['status']=='topology_uncertain'


def test_grade_separated_crossing_is_not_an_ordinary_intersection():
    result=evaluate(crossing(2))
    assert result['status']=='no_intersection' and result['grade_separated_conflicts']==1
    data=ordinary(type='P');data[0][0]['attributes']['t_level']=2
    result=evaluate(data)
    assert result['status']=='no_intersection' and result['grade_separated_conflicts']==1
    assert 'pseudo_crossing_node' in result['candidates'][0]['reasons']


@pytest.mark.parametrize('type',['E','C','N','J','B','T','D','Z'])
def test_end_name_change_and_artificial_split_nodes_are_not_intersections(type):
    data=ordinary(type=type);data[0][:]=[data[0][0],data[0][2]]
    result=evaluate(data)
    assert result['status']=='no_intersection' and result['candidates'][0]['status']=='rejected'


def test_nonintersection_type_with_four_way_topology_is_a_conflict_not_absence():
    result=evaluate(ordinary(type='Z'))
    assert result['status']=='topology_uncertain'
    assert 'nonintersection_type_conflicts_with_incident_topology' in result['candidates'][0]['reasons']


def test_pseudo_node_without_grade_separation_evidence_requires_review():
    assert evaluate(ordinary(type='P'))['status']=='topology_uncertain'
    data=ordinary(type='P');data[0][0]['attributes']['t_level']=None
    assert evaluate(data)['status']=='topology_uncertain'


@pytest.mark.parametrize('type',['X','R','Q',None,'OTHER'])
def test_unknown_or_multiple_use_node_type_is_uncertain(type):
    assert evaluate(ordinary(type=type))['status']=='topology_uncertain'


@pytest.mark.parametrize('field,value',[('tnode',None),('tnode',0),('tnode',1.0),('roadsegid',None)])
def test_null_or_noninteger_identifiers_fail_closed(field,value):
    data=ordinary();data[0][0]['attributes'][field]=value
    assert evaluate(data)['status']=='topology_uncertain'


def test_node_interid_is_not_objectid_and_endpoints_must_agree_with_node_geometry():
    data=ordinary();data[1][1][0]['attributes']['interid']=1001
    assert evaluate(data)['status']=='topology_uncertain'
    data=ordinary();data[1][1][0]['geometry']['x']+=.01
    result=evaluate(data)
    assert result['status']=='topology_uncertain'
    assert 'node_geometry_inconsistent' in result['candidates'][0]['reasons']


@pytest.mark.parametrize('value',[None,-1,10,True,'1'])
def test_missing_or_invalid_level_metadata_blocks_acceptance(value):
    data=ordinary();data[0][0]['attributes']['t_level']=value
    assert evaluate(data)['status']=='topology_uncertain'


def test_inconsistent_levels_at_shared_node_are_reported():
    data=ordinary();data[0][0]['attributes']['t_level']=2
    result=evaluate(data)
    assert result['status']=='topology_uncertain' and result['grade_separated_conflicts']==1


@pytest.mark.parametrize('status',['A','R','T',None])
def test_unconstructed_status_cannot_be_accepted(status):
    data=ordinary();data[0][0]['attributes']['segstat']=status
    assert evaluate(data)['status']=='topology_uncertain'


def test_direction_and_structured_name_conflicts_are_not_relaxed():
    data=ordinary()
    assert classify_pair('N A ST','B ST',*data)['status']=='street_not_found'
    data[0][0]['attributes']['rd30pred']='N'
    assert not street_matches(data[0][0],'A ST')
    assert canonical('WEST A STREET')==canonical('W A ST')
    assert canonical('A ST')!=canonical('A AVE')


def test_missing_road_and_invalid_source_descriptions_are_explicit():
    assert classify_pair('A ST','C ST',*ordinary())['status']=='street_not_found'
    assert classify_pair('A ST','A ST',*ordinary())['status']=='invalid_source'
    assert classify_pair('A ST','FT N OF B ST',*ordinary())['status']=='invalid_source'
    assert classify_pair('A ST','B ST',*ordinary(),provider_error='http_failure')['status']=='provider_error'
    assert classify_pair('A ST','B ST',*ordinary(),source_consistent=False)['status']=='invalid_source'


@pytest.mark.parametrize('point',[[None,32.8],[-117.2,float('nan')],[True,32.8],[32.8,-117.2],[-74,40.7]])
def test_invalid_or_out_of_scope_node_coordinates(point):
    data=ordinary();data[1][1][0]['geometry']=dict(x=point[0],y=point[1])
    assert evaluate(data)['status']=='topology_uncertain' and projected(point) is None


def test_missing_segment_geometry_and_incomplete_incidence_are_uncertain():
    data=ordinary();data[0][0]['geometry']=None
    assert evaluate(data)['status']=='topology_uncertain'
    data=ordinary();data[2][1]=data[2][1][:2]
    assert evaluate(data)['status']=='topology_uncertain'
    data=ordinary();data[2].clear()
    assert evaluate(data)['status']=='topology_uncertain'


def test_road_pair_deduplication_preserves_direction_and_name_components():
    assert pair_key('A ST','B ST')==pair_key('B STREET','A STREET')
    assert pair_key('A ST','B ST')!=pair_key('N A ST','B ST')
    assert 'CAMINO DEL NORTE' in road_names_variants('CAM DEL NORTE')
    assert pair_key('MT SOLEDAD RD','A ST')!=pair_key('MOUNT SOLEDAD RD','A ST')


def test_sample_is_reproducible_and_not_selected_by_provider_success():
    rows=[dict(report_id=str(i),primary_street='A ST' if i%2 else 'N A ST',intersecting_street='B ST',
        police_beat=str(100+(i%9)*100),raw_record={'ADDRESS_SFX_INTERSECTING':'ST'}) for i in range(200)]
    a=groups_sample(deepcopy(rows));b=groups_sample(list(reversed(deepcopy(rows))))
    assert len(a)==80 and [r['report_id'] for r in a]==[r['report_id'] for r in b]
    assert {r['directional_components'] for r in a}=={True,False}
    assert len({r['area_proxy'] for r in a})==9


@pytest.mark.parametrize('payload',[dict(error=dict(code=500)),dict(exceededTransferLimit=True),[],None])
def test_rest_errors_and_truncation_fail_closed(tmp_path,payload):
    capture=Capture(tmp_path,live=True)
    response=MagicMock();response.__enter__.return_value.read.return_value=json.dumps(payload).encode()
    opener=MagicMock();opener.open.return_value=response
    with patch('backend.verification.intersection_feasibility.study.build_opener',return_value=opener):
        with pytest.raises(EvidenceError):capture.query('https://example.test/query',{'f':'json'},'test')
    assert capture.new_requests==1


def test_http_failure_is_cached_and_no_uncontrolled_retry(tmp_path):
    capture=Capture(tmp_path,live=True)
    opener=MagicMock();opener.open.side_effect=HTTPError('https://example.test',503,'failed',{},None)
    with patch('backend.verification.intersection_feasibility.study.build_opener',return_value=opener):
        for _ in range(2):
            with pytest.raises(EvidenceError):capture.query('https://example.test/query',{'f':'json'},'test')
    assert opener.open.call_count==capture.new_requests==1


def test_offline_cache_and_strict_request_cap_prevent_network(tmp_path):
    capture=Capture(tmp_path)
    with patch('backend.verification.intersection_feasibility.study.build_opener') as http:
        with pytest.raises(EvidenceError):capture.query('https://example.test',{},'test')
    http.assert_not_called()
    capture.live=True;capture.ledger['attempts']=[{}]*64
    with pytest.raises(EvidenceError):capture.query('https://example.test',{},'test')


@pytest.mark.parametrize('payload',[dict(spatialReference=dict(wkid=2230),features=[]),
    dict(spatialReference=None,features=[]),dict(spatialReference='invalid',features=[]),
    dict(spatialReference=dict(wkid=4326),features=[None])])
def test_rest_crs_and_feature_shape_are_verified(payload):
    with pytest.raises(EvidenceError):features(payload)


def test_independent_validation_selection_includes_hard_cases():
    results=[]
    for i in range(30):
        r=evaluate(ordinary(i+1))
        r['pair_id']=str(i)
        if i==0:r['status']='multiple_candidates'
        if i==1:r['grade_separated_conflicts']=1;r['status']='no_intersection'
        results.append(r)
    selected=independent_selection(results)
    assert len(selected)==20 and results[0] in selected and results[1] in selected


def test_complete_broader_node_cache_is_reused_without_extra_requests(tmp_path):
    capture=Capture(tmp_path)
    roads,nodes,incident=ordinary()
    payloads=[('nodes',dict(url='https://example.test?where=interid+IN+(1,2)',response=dict(spatialReference=dict(wkid=4326),features=nodes[1]))),
        ('incidence',dict(url='https://example.test?where=fnode+IN+(1,2)+OR+tnode+IN+(1,2)',response=dict(spatialReference=dict(wkid=4326),features=roads)))]
    for kind,record in payloads:
        with gzip.open(tmp_path/'cache'/f'{kind}-scope.json.gz','wt',encoding='utf-8') as file:json.dump(record,file)
    with patch('backend.verification.intersection_feasibility.study.build_opener') as http:
        cached_nodes,cached_incident=node_evidence([('A ST','B ST'),('A ST','A ST')],roads,capture)
    http.assert_not_called()
    assert set(cached_nodes)=={1} and len(cached_incident[1])==4


def test_population_counts_are_workload_ceiling_not_a_recovery_estimate():
    rows=[dict(primary_street='A ST',intersecting_street='B ST'),dict(primary_street='B STREET',intersecting_street='A STREET'),
        dict(primary_street='A ST',intersecting_street='A ST')]
    result=population_summary(rows,{}, {})
    assert result['records']==3 and result['source_screen_passed_records']==2
    assert result['source_screen_passed_unique_pairs']==1
    assert result['invalid_source_reasons']=={'same_road_twice':1}
