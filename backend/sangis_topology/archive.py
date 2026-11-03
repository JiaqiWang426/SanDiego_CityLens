"""Compare imported source attributes against every frozen Stage A REST feature."""
import gzip
import json

from backend.verification.intersection_feasibility.study import HERE as STAGE_A, connection
from .acquire import read, save


def compare(sid,output):
    expected={'road_segments':{},'road_nodes':{}}
    for path in (STAGE_A/'cache').glob('*.json.gz'):
        table='road_segments' if path.name.startswith(('road_names-','incidence-')) else 'road_nodes' if path.name.startswith('nodes-') else None
        if table is None:
            continue
        with gzip.open(path,'rt',encoding='utf-8') as stream:
            payload=json.load(stream)['response']
        for feature in payload['features']:
            a=feature['attributes']
            old=expected[table].setdefault(a['objectid'],a)
            if old!=a:
                raise ValueError('Conflicting frozen Stage A attributes')
    result={'snapshot_id':sid,'layers':{},'source_geometry_comparison':'Archived REST WGS84 geometry vs local native transform is quantified at all evaluated Stage A candidate nodes in stage-a-replay.json.'}
    with connection() as conn:
        manifest=conn.execute('SELECT manifest FROM citylens_ref.snapshots WHERE snapshot_id=%s',(sid,)).fetchone()['manifest']
        for table,layer in [('road_segments','Roads_All'),('road_nodes','Roads_Intersection')]:
            rows=conn.execute(f'SELECT objectid,attributes FROM citylens_ref.{table} WHERE snapshot_id=%s AND objectid=ANY(%s)',(sid,list(expected[table]))).fetchall()
            actual={r['objectid']:r['attributes'] for r in rows}
            mismatches=[{'objectid':oid,'differing_fields':[k for k,v in fields.items() if actual.get(oid,{}).get(k)!=v]}
                        for oid,fields in expected[table].items() if oid not in actual or any(actual[oid].get(k)!=v for k,v in fields.items())]
            result['layers'][layer]={'archived_features':len(expected[table]),'local_features_found':len(actual),
                'attribute_mismatches':mismatches,'same_editing_info':read(STAGE_A/(layer+'-metadata.json')).get('editingInfo')==manifest['layers'][layer]['source_editing_info']}
    result['passed']=all(not layer['attribute_mismatches'] and layer['same_editing_info'] for layer in result['layers'].values())
    save(output/'rest-archive-comparison.json',result)
    return result
