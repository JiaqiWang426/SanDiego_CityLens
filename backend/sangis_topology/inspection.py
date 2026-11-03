"""Read frozen independent evidence without changing matches or adding aliases."""
import gzip
import json
from urllib.parse import parse_qs,urlparse

from backend.verification.intersection_feasibility.topology import canonical,geometry,projected
from backend.scripts.audit_historical_locations import point_segment_distance
from backend.verification.intersection_feasibility.study import write_csv
from .acquire import read,save


def inspect(output):
    checks=read(output/'independent-validation.json')
    pairs={r['pair_id']:r for r in read(output/'pair-results.json')}
    captures={}
    for path in (output/'independent-evidence'/'cache').glob('*.json.gz'):
        with gzip.open(path,'rt',encoding='utf-8') as stream:
            record=json.load(stream)
        box=json.loads(parse_qs(urlparse(record['url']).query)['geometry'][0])
        captures[(round((box['xmin']+box['xmax'])/2,10),round((box['ymin']+box['ymax'])/2,10))]=record
    notes=[]
    crosswalk=[]
    for pair in checks:
        result=pairs[pair['pair_id']]
        nodes={c['node_id']:c for c in result['candidates']}
        for check in pair['checks']:
            candidate=nodes[check['node_id']]
            for report_id in result['report_ids']:
                crosswalk.append({'report_id':report_id,'pair_id':pair['pair_id'],'node_id':check['node_id'],
                    'pair_classification':pair['study_status'],'topology_outcome':check['topology_outcome'],
                    'independent_outcome':check['status'],'road_checks':check.get('road_checks',{}),
                    'historical_or_grade_ground_truth':'not_verified'})
            # Examine all uncorroborated accepted nodes plus corroborated controls
            # and the hard cases; no topology acceptance decision is changed.
            record=captures[(round(candidate['longitude'],10),round(candidate['latitude'],10))]
            point=projected([candidate['longitude'],candidate['latitude']])
            observed={}
            for feature in record['response']['results']:
                attrs=feature.get('attributes') or {}
                name=attrs.get('NAME') or attrs.get('Name') or '(unnamed)'
                coords=geometry(feature)
                if coords:
                    distance=min(point_segment_distance(point,a,b) for a,b in zip(coords,coords[1:]))
                    observed[name]=min(distance,observed.get(name,float('inf')))
            missing=[s for s,v in check.get('road_checks',{}).items() if v['matching_features']==0]
            disagreements=[{'road':s,'distance_m':v['distance_m']} for s,v in check.get('road_checks',{}).items()
                           if v['matching_features']>0 and (v['distance_m'] is None or v['distance_m']>30)]
            notes.append({'pair_id':pair['pair_id'],'primary':pair['primary'],'cross':pair['cross'],
                          'classification':pair['study_status'],'node_id':check['node_id'],'topology_outcome':check['topology_outcome'],
                          'independent_outcome':check['status'],'missing_exact_names':missing,'named_road_spatial_disagreements':disagreements,
                          'nearest_observed_names':[{'name':n,'canonical':canonical(n),'distance_m':round(d,3)} for n,d in sorted(observed.items(),key=lambda item:(item[1],item[0]))[:10]],
                          'interpretation':'Spatial diagnostic only. No fuzzy aliases, extra ramps or inferred historical connectivity accepted.'})
    save(output/'independent-inspection.json',notes)
    write_csv(output/'independent-candidate-crosswalk.csv',crosswalk)
    accepted=[n for n in notes if n['classification']=='accepted_candidate' and n['topology_outcome']=='supported']
    summary={'accepted_review_nodes':len(accepted),
             'uncorroborated_accepted_nodes':sum(n['independent_outcome']!='both_roads_spatially_corroborated' for n in accepted),
             'accepted_nodes_missing_exact_names':sum(bool(n['missing_exact_names']) for n in accepted),
             'accepted_nodes_with_named_road_spatial_disagreement':sum(bool(n['named_road_spatial_disagreements']) for n in accepted),
             'corroborated_grade_conflict_candidate_checks':sum(c['status']=='both_roads_spatially_corroborated' for r in checks if r['grade_conflicts'] for c in r['checks']),
             'warning':'Neither an uncorroborated name nor a corroborated grade-separated crossing is historical collision ground truth. Do not interpret the sample failure fraction as a false-positive rate.'}
    save(output/'independent-inspection-summary.json',summary)
    return summary
