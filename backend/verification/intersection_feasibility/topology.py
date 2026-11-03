"""Conservative intersection feasibility only; no persistence or geocoding worker.

Exact node identity is primary evidence. Coordinates validate identity, never
create a node join. Existing historical street normalization is reused unchanged.
"""
from collections import defaultdict
import math
import re

from backend.census_addresses import census_street
from backend.scripts.audit_historical_locations import TRANSFORM

STATUSES={'accepted_candidate','multiple_candidates','topology_uncertain','street_not_found',
          'no_intersection','invalid_source','provider_error'}
POINT_TOLERANCE_M=2  # Agreement check only; never snap/join/choose nearest.


def canonical(street):return census_street(street)


def pair_key(primary,cross):return tuple(sorted((canonical(primary),canonical(cross))))


def invalid_source(primary,cross):
    if not primary or not cross:return 'missing_street'
    if canonical(primary)==canonical(cross):return 'same_road_twice'
    for street in (primary,cross):
        if re.search(r'\b(?:FT|FEET|PARKING LOT|BLOCK|NCL|SCL|ECL|WCL)\b',street):return 'descriptive_location_not_two_roads'
        if any(c in street for c in ['&','/','(',')']):return 'compound_or_incomplete_street_description'
    return None


def identifier(value):
    return value if type(value) is int and value>0 else None


def level(value):return value if type(value) is int and 0<=value<=9 else None


def projected(point):
    if not isinstance(point,(list,tuple)) or len(point)<2:return None
    x,y=point[:2]
    if any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) for v in (x,y)):return None
    if not (-117.6<=x<=-116.8 and 32.5<=y<=33.2):return None
    return TRANSFORM.transform(x,y)


def geometry(feature):
    if not isinstance(feature,dict):return None
    paths=(feature.get('geometry') or {}).get('paths') if isinstance(feature.get('geometry'),dict) else None
    if not isinstance(paths,list) or len(paths)!=1 or not isinstance(paths[0],list) or len(paths[0])<2:return None
    points=[projected(p) for p in paths[0]]
    return points if all(p is not None for p in points) and any(p!=points[0] for p in points[1:]) else None


def street_matches(feature,expected):
    a=feature.get('attributes',{}) if isinstance(feature,dict) else {}
    if not isinstance(a,dict):return False
    full=a.get('rd30full')
    fields=[a.get(k) or '' for k in ['rd30pred','rd30name','rd30sfx','rd30postd']]
    if not isinstance(full,str) or not all(isinstance(v,str) for v in fields) or not fields[1]:return False
    return canonical(full)==canonical(expected)==canonical(' '.join(fields))


def endpoints(feature):
    a=feature.get('attributes',{})
    points=geometry(feature)
    if points is None:return []
    return [(identifier(a.get('fnode')),level(a.get('f_level')),points[0],'from'),
            (identifier(a.get('tnode')),level(a.get('t_level')),points[-1],'to')]


def segment_crossing(a,b,c,d):
    """One 2D intersection, including endpoints; collinear overlap is uncertain."""
    ax,ay=b[0]-a[0],b[1]-a[1];bx,by=d[0]-c[0],d[1]-c[1]
    determinant=ax*by-ay*bx
    if abs(determinant)<1e-9:
        # Collinear overlap is secondary uncertainty, never an invented node.
        length2=ax*ax+ay*ay
        if not length2 or abs((c[0]-a[0])*ay-(c[1]-a[1])*ax)>1e-7:return None
        t1=((c[0]-a[0])*ax+(c[1]-a[1])*ay)/length2
        t2=((d[0]-a[0])*ax+(d[1]-a[1])*ay)/length2
        low,high=max(0,min(t1,t2)),min(1,max(t1,t2))
        if low>high+1e-9:return None
        t=(low+high)/2
        return a[0]+t*ax,a[1]+t*ay
    dx,dy=c[0]-a[0],c[1]-a[1]
    t=(dx*by-dy*bx)/determinant;u=(dx*ay-dy*ax)/determinant
    if -1e-9<=t<=1+1e-9 and -1e-9<=u<=1+1e-9:
        return (a[0]+t*ax,a[1]+t*ay)
    return None


def crossings(first,second):
    found=[]
    for a in first:
        ga=geometry(a)
        if ga is None:continue
        for b in second:
            gb=geometry(b)
            if gb is None:continue
            # Cheap per-feature bounding-box pruning before segment pairs.
            if max(p[0] for p in ga)<min(p[0] for p in gb) or max(p[0] for p in gb)<min(p[0] for p in ga) or max(p[1] for p in ga)<min(p[1] for p in gb) or max(p[1] for p in gb)<min(p[1] for p in ga):continue
            for p,q in zip(ga,ga[1:]):
                for r,s in zip(gb,gb[1:]):
                    point=segment_crossing(p,q,r,s)
                    if point is not None and not any(math.dist(point,old)<.05 for old in found):found.append(point)
    return found


def shared_nodes(first,second):
    left={node for f in first for node,_,_,_ in endpoints(f) if node}
    right={node for f in second for node,_,_,_ in endpoints(f) if node}
    return left&right


def classify_pair(primary,cross,roads,nodes,incidence,*,provider_error=None,source_consistent=True):
    """nodes maps interid -> complete feature list; incidence maps node -> roads.

    Inputs must be complete validated REST result sets. No truncation/pagination
    approximation is accepted by the transport. Null endpoint identity or missing
    geometry can hide another candidate, so it prevents a unique acceptance.
    """
    result=dict(primary=primary,cross=cross,status='no_intersection',reason=None,
        candidates=[],shared_node_count=0,geometric_crossings=0,grade_separated_conflicts=0,diagnostics=[])
    reason=invalid_source(primary,cross)
    if reason:return dict(result,status='invalid_source',reason=reason)
    if not source_consistent:return dict(result,status='invalid_source',reason='stored_streets_disagree_with_original_components')
    if provider_error:return dict(result,status='provider_error',reason=provider_error)
    first=[r for r in roads if street_matches(r,primary)]
    second=[r for r in roads if street_matches(r,cross)]
    if not first or not second:return dict(result,status='street_not_found',reason='complete_road_components_not_found')
    for feature in first+second:
        if geometry(feature) is None:result['diagnostics'].append('invalid_or_out_of_envelope_road_geometry')
        if any(node is None for node,_,_,_ in endpoints(feature)):result['diagnostics'].append('missing_endpoint_identity')
        if identifier(feature.get('attributes',{}).get('roadsegid')) is None:result['diagnostics'].append('missing_segment_identity')
    result['diagnostics']=sorted(set(result['diagnostics']))
    faulty=bool(result['diagnostics'])
    common=shared_nodes(first,second);geometric=crossings(first,second)
    result.update(shared_node_count=len(common),geometric_crossings=len(geometric))
    supports=[];uncertain=faulty
    supported_points=[]
    for node in sorted(common):
        features=nodes.get(node,[])
        candidate=dict(node_id=node,status='topology_uncertain',reasons=[],node_type=None,
            longitude=None,latitude=None,incident_segment_ids=[],primary_segment_ids=[],cross_segment_ids=[],
            endpoint_levels=[],max_endpoint_distance_m=None)
        if len(features)!=1:
            candidate['reasons'].append('missing_or_duplicate_node_record');uncertain=True
            result['candidates'].append(candidate);continue
        feature=features[0];attrs=feature.get('attributes') or {};g=feature.get('geometry') or {}
        point=projected([g.get('x'),g.get('y')])
        candidate['node_type']=attrs.get('type')
        if point is None or identifier(attrs.get('interid'))!=node:
            candidate['reasons'].append('invalid_node_geometry_or_identity');uncertain=True
            result['candidates'].append(candidate);continue
        candidate.update(longitude=g['x'],latitude=g['y'])
        incident=incidence.get(node)
        if incident is None:
            candidate['reasons'].append('missing_incident_segments');uncertain=True
            result['candidates'].append(candidate);continue
        segment_ids=[identifier(f.get('attributes',{}).get('roadsegid')) for f in incident]
        candidate['incident_segment_ids']=sorted({id for id in segment_ids if id})
        matching_a=[f for f in incident if street_matches(f,primary)]
        matching_b=[f for f in incident if street_matches(f,cross)]
        candidate['primary_segment_ids']=sorted({id for f in matching_a if (id:=identifier(f['attributes'].get('roadsegid')))})
        candidate['cross_segment_ids']=sorted({id for f in matching_b if (id:=identifier(f['attributes'].get('roadsegid')))})
        if not matching_a or not matching_b:candidate['reasons'].append('both_roads_not_incident')
        observations=[]
        for road in incident:
            ends=[(l,p) for id,l,p,side in endpoints(road) if id==node]
            if not ends:candidate['reasons'].append('invalid_incident_endpoint');continue
            observations.extend((l,math.dist(point,p),road) for l,p in ends)
        levels=[l for l,_,_ in observations]
        candidate['endpoint_levels']=sorted(set(l for l in levels if l is not None))
        candidate['max_endpoint_distance_m']=round(max((d for _,d,_ in observations),default=0),4)
        if not observations or any(l is None for l in levels):candidate['reasons'].append('missing_or_invalid_endpoint_level')
        if len(set(l for l in levels if l is not None))>1:
            candidate['reasons'].append('grade_separated_levels');result['grade_separated_conflicts']+=1
        if any(d>POINT_TOLERANCE_M for _,d,_ in observations):candidate['reasons'].append('node_geometry_inconsistent')
        if any(id is None for id in segment_ids) or len(segment_ids)!=len(set(segment_ids)):
            candidate['reasons'].append('invalid_or_duplicate_segment_identity')
        if any(f.get('attributes',{}).get('segstat') not in {'C','M'} for f in incident):candidate['reasons'].append('road_status_not_confirmed_constructed')
        if any(f.get('attributes',{}).get('segclass') not in {'1','2','3','4','5','6','7','8','9','A','M','Z'} for f in incident):
            candidate['reasons'].append('road_class_requires_review')
        node_type=attrs.get('type')
        if node_type in {'C','E','N','J','B','T','D','Z'}:
            if matching_a and matching_b and len(set(segment_ids))>=3:
                candidate['reasons'].append('nonintersection_type_conflicts_with_incident_topology')
            else:
                candidate['status']='rejected';candidate['reasons'].append('not_a_physical_intersection_type')
        elif node_type=='P':
            candidate['reasons'].append('pseudo_crossing_node')
            if 'grade_separated_levels' in candidate['reasons'] and not any(l is None for l in levels):candidate['status']='rejected'
        elif node_type!='I':candidate['reasons'].append('node_type_requires_review')
        if len(set(segment_ids))<3:candidate['reasons'].append('fewer_than_three_incident_segments')
        if candidate['status']!='rejected':
            if candidate['reasons']:uncertain=True
            else:
                candidate['status']='supported';supports.append(node);supported_points.append(point)
        result['candidates'].append(candidate)
    # A second geometric crossing not explained by a known shared endpoint is
    # unresolved topology, even if another physical node was supported.
    common_points=[projected([n['geometry']['x'],n['geometry']['y']]) for id in common for n in nodes.get(id,[])
        if isinstance(n.get('geometry'),dict) and projected([n['geometry'].get('x'),n['geometry'].get('y')]) is not None]
    unexplained=any(not any(math.dist(point,known)<=POINT_TOLERANCE_M for known in common_points) for point in geometric)
    constant_levels=[]
    for group in (first,second):
        values={level(f.get('attributes',{}).get(k)) for f in group for k in ['f_level','t_level']}
        constant_levels.append(next(iter(values)) if len(values)==1 and None not in values else None)
    documented_grade_crossing=(bool(geometric) and not common and None not in constant_levels
        and constant_levels[0]!=constant_levels[1])
    if documented_grade_crossing:result['grade_separated_conflicts']+=1
    if unexplained and not documented_grade_crossing:
        uncertain=True;result['diagnostics'].append('geometric_crossing_without_verified_shared_node')
    if len(supports)>1:result.update(status='multiple_candidates',reason='multiple_supported_intersection_nodes')
    elif uncertain:result.update(status='topology_uncertain',reason='incomplete_or_conflicting_topology_evidence')
    elif len(supports)==1:result.update(status='accepted_candidate',reason='one_supported_physical_node')
    else:result.update(status='no_intersection',reason='no_supported_physical_node')
    return result
