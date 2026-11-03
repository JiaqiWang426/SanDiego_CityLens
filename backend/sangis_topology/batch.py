"""Local resolver and resumable dry run. All database access here is read only."""
from collections import Counter, defaultdict
import json
import math
from pathlib import Path

from backend.collision_addresses import normalize_collision_address, clean_text
from backend.verification.intersection_feasibility import topology as stage_a
from backend.verification.intersection_feasibility.study import connection, write_csv, HERE as STAGE_A
from .acquire import EvidenceError, digest, now, read, save, sha
from .database import reference_reader_guard

DEFAULT_OUTPUT = Path(__file__).resolve().parents[1] / 'verification' / 'intersection_batch'
GEOCODE_FIELDS = ['location','geocode_status','geocode_provider','geocode_precision','geocoded_at','geocode_details']
METHOD_VERSION = 'sangis-stage-a-local-v2'


def audit(conn):
    row = conn.execute('''SELECT count(*) AS records,count(location) AS located,
       md5(string_agg(md5(to_jsonb(c)::text),'' ORDER BY report_id)) AS all_rows,
       md5(string_agg(md5((to_jsonb(c)-%s::text[])::text),'' ORDER BY report_id)) AS source_fields,
       md5(string_agg(md5(to_jsonb(c)::text),'' ORDER BY report_id) FILTER(WHERE geocode_status='matched')) AS matched_rows,
       count(*) FILTER(WHERE geocode_status='matched') AS matched_count
       FROM citylens.traffic_collisions c''', (GEOCODE_FIELDS,)).fetchone()
    row['statuses'] = {r['geocode_status']:r['n'] for r in conn.execute('SELECT geocode_status,count(*) AS n FROM citylens.traffic_collisions GROUP BY 1')}
    return row


def archived_audit(conn):
    # Maintenance-only read: the Agent intentionally cannot read the raw archive.
    return conn.execute("SELECT count(*) AS records,md5(string_agg(md5(to_jsonb(s)::text),'' ORDER BY import_id,row_number)) AS digest FROM citylens.collision_source_rows s").fetchone()


def source_fingerprint(row):
    return digest({'version': 1, 'raw_address': {k:v for k,v in row['raw_record'].items() if k.startswith('ADDRESS_')},
                   **{k:row[k] for k in ('address_key','primary_street','intersecting_street')}})


def source_key(row):
    # Number, unknown ADDRESS_* fields and area qualifiers survive deduplication.
    # These are NOT assumed to disambiguate a road node or to be actual numbers.
    road_fields = {'ADDRESS_PD_PRIMARY','ADDRESS_ROAD_PRIMARY','ADDRESS_SFX_PRIMARY',
                   'ADDRESS_PD_INTERSECTING','ADDRESS_NAME_INTERSECTING','ADDRESS_SFX_INTERSECTING'}
    return digest({'version': 1, 'primary': stage_a.canonical(row['primary_street']),
                   'cross': stage_a.canonical(row['intersecting_street']),
                   'qualifiers': {k:clean_text(v) for k,v in row['raw_record'].items() if k.startswith('ADDRESS_') and k not in road_fields},
                   'police_beat': row.get('police_beat')})


def prepare(output):
    with connection() as conn:
        permissions = reference_reader_guard(conn)
        before = audit(conn)
        rows = conn.execute('''SELECT report_id,occurred_at,police_beat,primary_street,intersecting_street,address_key,raw_record
             FROM citylens.traffic_collisions WHERE geocode_status='pending' AND intersecting_street<>'' ORDER BY report_id''').fetchall()
        all_pending = before['statuses'].get('pending',0)
        for row in rows:
            row['occurred_at'] = row['occurred_at'].isoformat() if row['occurred_at'] else None
            normalized = normalize_collision_address(row['raw_record'])
            row['source_components_consistent'] = (normalized.primary_street == row['primary_street'] and normalized.intersecting_street == row['intersecting_street'] and normalized.address_key == row['address_key'])
            row['raw_record'] = {k:v for k,v in row['raw_record'].items() if k.startswith('ADDRESS_')}
            row['source_address_fingerprint'] = source_fingerprint(row)
            row['normalized_source_key'] = source_key(row)
        after = audit(conn)
    if before != after:
        raise EvidenceError('Collision data changed during input capture')
    value = {'captured_at':now(),'before':before,'records':rows,'excluded_pending_without_cross_street':all_pending-len(rows),
             'agent_reference_permissions':permissions}
    target = output / 'input.json'
    if target.exists():
        frozen = read(target)
        if frozen['before'] != before or frozen['records'] != rows:
            raise EvidenceError('Dry run inputs changed; use a new output directory')
        return frozen
    save(target,value)
    return value


def road_feature(row):
    g = row['geojson']
    return {'attributes': row['attributes'], 'geometry': {'paths': g['coordinates']} if g else None}


class LocalResolver:
    def __init__(self, conn, sid, pairs):
        snapshot = conn.execute('SELECT manifest,integrity FROM citylens_ref.snapshots WHERE snapshot_id=%s',(sid,)).fetchone()
        if not snapshot or not snapshot['integrity'] or not snapshot['integrity']['usable_for_conservative_proposals']:
            raise EvidenceError('Validated, complete reference snapshot required')
        self.manifest = snapshot['manifest']
        self.by_name = defaultdict(list)
        names = sorted({street for a,b in pairs if not stage_a.invalid_source(a,b) for street in (a,b)})
        roads = conn.execute('''SELECT attributes,ST_AsGeoJSON(geom_4326,15)::jsonb AS geojson
             FROM citylens_ref.road_segments WHERE snapshot_id=%s AND canonical_name=ANY(%s)
             AND ST_Intersects(geom_4326,ST_MakeEnvelope(-117.6,32.5,-116.8,33.2,4326)) ORDER BY roadsegid''',(sid,names)).fetchall()
        self.roads = [road_feature(row) for row in roads]
        print(f'Loaded {len(self.roads)} complete named-road segments from local PostGIS',flush=True)
        for feature in self.roads:
            self.by_name[stage_a.canonical(feature['attributes']['rd30full'])].append(feature)
        ids = sorted({node for a,b in pairs if not stage_a.invalid_source(a,b)
                      for node in stage_a.shared_nodes(self.by_name[a],self.by_name[b])})
        self.nodes, self.incidence = defaultdict(list), defaultdict(list)
        for row in conn.execute('''SELECT interid,attributes,ST_AsGeoJSON(geom_4326,15)::jsonb AS geojson
               FROM citylens_ref.road_nodes WHERE snapshot_id=%s AND interid=ANY(%s) ORDER BY interid''',(sid,ids)):
            g = row['geojson']
            self.nodes[row['interid']].append({'attributes':row['attributes'],'geometry':{'x':g['coordinates'][0],'y':g['coordinates'][1]} if g else None})
        relevant = set(ids)
        for row in conn.execute('''SELECT attributes,ST_AsGeoJSON(geom_4326,15)::jsonb AS geojson FROM citylens_ref.road_segments
              WHERE snapshot_id=%s AND (fnode=ANY(%s) OR tnode=ANY(%s)) ORDER BY roadsegid''',(sid,ids,ids)):
            feature = road_feature(row)
            for node in {feature['attributes']['fnode'],feature['attributes']['tnode']} & relevant:
                self.incidence[node].append(feature)
        self.loaded = {'named_segments':len(self.roads),'shared_node_ids':len(ids),'node_records':sum(map(len,self.nodes.values()))}
        print(f'Loaded {len(ids)} shared node identities and their complete incident segments',flush=True)

    def resolve(self, primary, cross, *, source_consistent=True):
        roads = self.by_name.get(primary,[]) + self.by_name.get(cross,[])
        # Missing incidence must stay missing, not a fabricated complete empty set.
        result = stage_a.classify_pair(primary,cross,roads,self.nodes,self.incidence,source_consistent=source_consistent)
        for candidate in result['candidates']:
            candidate['source_segments'] = [{k:f['attributes'].get(k) for k in
                ('roadsegid','rd30full','fnode','tnode','f_level','t_level','segclass','segstat','funclass','oneway')}
                for f in self.incidence.get(candidate['node_id'],[])]
        return result


def replay_stage_a(resolver, output):
    archived = read(STAGE_A / 'pair-results.json')
    comparisons = []
    for expected in archived:
        a,b = stage_a.pair_key(expected['primary'],expected['cross'])
        actual = resolver.resolve(a,b)
        old_nodes = {c['node_id']:c for c in expected['candidates']}
        new_nodes = {c['node_id']:c for c in actual['candidates']}
        discrepancy = max((abs(old_nodes[n][k]-new_nodes[n][k]) for n in old_nodes.keys()&new_nodes.keys()
                           for k in ('longitude','latitude') if old_nodes[n][k] is not None and new_nodes[n][k] is not None), default=0)
        distance = max((math.dist(stage_a.projected([old_nodes[n]['longitude'],old_nodes[n]['latitude']]),
                         stage_a.projected([new_nodes[n]['longitude'],new_nodes[n]['latitude']]))
                        for n in old_nodes.keys()&new_nodes.keys() if old_nodes[n]['longitude'] is not None and new_nodes[n]['longitude'] is not None), default=0)
        comparisons.append({'pair_id':expected['pair_id'],'archived_status':expected['status'],'local_status':actual['status'],
                            'same_node_decisions':{n:c['status'] for n,c in old_nodes.items()}=={n:c['status'] for n,c in new_nodes.items()},
                            'same_node_evidence':{n:{k:c[k] for k in ('node_type','reasons','incident_segment_ids','primary_segment_ids','cross_segment_ids','endpoint_levels')} for n,c in old_nodes.items()}
                                =={n:{k:c[k] for k in ('node_type','reasons','incident_segment_ids','primary_segment_ids','cross_segment_ids','endpoint_levels')} for n,c in new_nodes.items()},
                            'coordinate_delta_degrees':discrepancy,'coordinate_delta_m':distance})
    result = {'pairs':len(comparisons),'same_statuses':sum(c['archived_status']==c['local_status'] for c in comparisons),
              'same_node_decisions':sum(c['same_node_decisions'] for c in comparisons),
              'same_node_evidence':sum(c['same_node_evidence'] for c in comparisons),
              'max_coordinate_delta_degrees':max(c['coordinate_delta_degrees'] for c in comparisons),
              'max_coordinate_delta_m':max(c['coordinate_delta_m'] for c in comparisons),'comparisons':comparisons,
              'coordinate_comparison_ceiling_m':stage_a.POINT_TOLERANCE_M,
              'coordinate_comparison_basis':'Native EPSG:2230 transformed with PostGIS/PROJ NAD83 to WGS84 (1); archived provider outSR coordinates differ slightly. Same node and classification must match exactly. Coordinate comparison uses the existing Stage A 2m consistency ceiling; it does not change matching, connectivity or accuracy claims.'}
    result['passed'] = result['pairs']==result['same_statuses']==result['same_node_decisions']==result['same_node_evidence'] and result['max_coordinate_delta_m']<=stage_a.POINT_TOLERANCE_M
    save(output / 'stage-a-replay.json',result)
    return result


def candidate_rows(rows, results, sid, manifest):
    for row in rows:
        pair = stage_a.pair_key(row['primary_street'],row['intersecting_street'])
        result = results[pair]
        if not row['source_components_consistent']:
            result = stage_a.classify_pair(*pair,[],{},{},source_consistent=False)
        for candidate in result['candidates'] or [None]:
            yield {'report_id':row['report_id'],'source_address_fingerprint':row['source_address_fingerprint'],
                   'address_key':row['address_key'],'normalized_source_key':row['normalized_source_key'],
                   'normalized_primary_road':stage_a.canonical(row['primary_street']),
                   'normalized_cross_road':stage_a.canonical(row['intersecting_street']),
                   'pair_id':digest(pair),'classification':result['status'],
                   'reason':result['reason'],'diagnostics':result['diagnostics'],
                   'distinct_valid_candidate_nodes':sum(c['status']=='supported' for c in result['candidates']),
                   'node_id':candidate['node_id'] if candidate else None,
                   'node_type':candidate['node_type'] if candidate else None,
                   'max_endpoint_distance_m':candidate['max_endpoint_distance_m'] if candidate else None,
                   'longitude':candidate['longitude'] if candidate else None,'latitude':candidate['latitude'] if candidate else None,
                   'primary_segment_ids':candidate['primary_segment_ids'] if candidate else [],
                   'cross_segment_ids':candidate['cross_segment_ids'] if candidate else [],
                   'incident_segment_ids':candidate['incident_segment_ids'] if candidate else [],
                   'road_segment_evidence':candidate.get('source_segments',[]) if candidate else [],
                   'topology_outcome':candidate['status'] if candidate else 'unresolved',
                   'topology_reasons':candidate['reasons'] if candidate else [result['reason']],
                   'level_outcome':('compatible_documented_levels' if candidate and candidate['status']=='supported' else 'unresolved_or_rejected'),
                   'endpoint_levels':candidate['endpoint_levels'] if candidate else [],
                   'road_source_status':'constructed_or_maintained' if candidate and candidate['status']=='supported' else 'requires_review',
                   'precision':'intersection_approximate','source':'SanGIS Roads_All + Roads_Intersection',
                   'snapshot_id':sid,'source_versions':{k:v['source_editing_info'] for k,v in manifest['layers'].items()},
                   'method_version':METHOD_VERSION,'srid':4326,
                   'temporal_uncertainty':'Current reference does not verify collision-date intersection or exact collision position.',
                   'location_qualifiers':'Source block/beat retained in input and source key; not independently verified disambiguators.',
                   'deployment_approved':False}


def dry_run(sid, output=DEFAULT_OUTPUT):
    output = Path(output)
    output.mkdir(parents=True,exist_ok=True)
    frozen = prepare(output)
    rows = frozen['records']
    groups = defaultdict(list)
    for row in rows:
        groups[stage_a.pair_key(row['primary_street'],row['intersecting_street'])].append(row)
    pairs = sorted(set(groups) | {stage_a.pair_key(r['primary'],r['cross']) for r in read(STAGE_A/'pair-results.json')})
    with connection() as conn:
        resolver = LocalResolver(conn,sid,pairs)
    replay = replay_stage_a(resolver,output)
    from .archive import compare
    archive = compare(sid,output)
    # Stage A mismatch blocks deployment recommendation; analysis remains visible.
    checkpoint = output / 'checkpoints'
    checkpoint.mkdir(exist_ok=True)
    results = {}
    for index,(pair,records) in enumerate(sorted(groups.items())):
        path = checkpoint / (digest({'pair':pair,'snapshot':sid,'method':METHOD_VERSION}) + '.json')
        if path.exists():
            result = read(path)
            if (result['primary'],result['cross']) != pair:
                raise EvidenceError('Checkpoint identity mismatch')
        else:
            result = resolver.resolve(*pair)
            result.update(pair_id=digest(pair),report_ids=[r['report_id'] for r in records])
            save(path,result)
        results[pair] = result
        if index % 250 == 0:
            print(f'Resolved {index+1}/{len(groups)} distinct normalized pairs',flush=True)
    save(output/'pair-results.json',list(results.values()))
    candidates = list(candidate_rows(rows,results,sid,resolver.manifest))
    temporary = output / 'candidates.jsonl.tmp'
    with temporary.open('w',encoding='utf-8',newline='\n') as stream:
        for row in candidates:
            stream.write(json.dumps(row,sort_keys=True,separators=(',',':'),allow_nan=False)+'\n')
    temporary.replace(output/'candidates.jsonl')
    write_csv(output/'candidates.csv',candidates)
    accepted_ids = {c['report_id'] for c in candidates if c['classification']=='accepted_candidate'}
    pair_counts = Counter(r['status'] for r in results.values())
    by_report = {c['report_id']:c['classification'] for c in candidates}
    report_counts = Counter(by_report.values())
    with connection() as conn:
        after = audit(conn)
        guard = reference_reader_guard(conn)
    if after != frozen['before']:
        raise EvidenceError('Collision fingerprint changed since frozen input')
    summary = {'snapshot_id':sid,'created_at':now(),'method_version':METHOD_VERSION,
               'selected_pending_reports':len(rows),'complete_road_pair_reports':sum(bool(r['primary_street']) for r in rows),
               'excluded_pending_without_cross_street':frozen['excluded_pending_without_cross_street'],
               'raw_complete_field_pairs':len({tuple(sorted((r['primary_street'],r['intersecting_street']))) for r in rows if r['primary_street']}),
               'distinct_normalized_pairs':len(groups),'distinct_complete_normalized_pairs':sum(bool(a and b) for a,b in groups),
               'qualified_source_keys':len({r['normalized_source_key'] for r in rows}),
               'pair_classifications':dict(pair_counts),'report_classifications':dict(report_counts),
               'supported_candidate_nodes':sum(c['status']=='supported' for r in results.values() for c in r['candidates']),
               'rejected_candidate_nodes':sum(c['status']=='rejected' for r in results.values() for c in r['candidates']),
               'uncertain_candidate_nodes':sum(c['status']=='topology_uncertain' for r in results.values() for c in r['candidates']),
               'accepted_proposal_reports':len(accepted_ids),'collision_updates':0,
               'existing_matched':after['matched_count'],'denominator':after['records'],
               'deployed_coverage_percent':100*after['matched_count']/after['records'],
               'proposed_additional_percentage_points':100*len(accepted_ids)/after['records'],
               'hypothetical_coverage_percent':100*(after['matched_count']+len(accepted_ids))/after['records'],
               'stage_a_replay_passed':replay['passed'],'stage_a_archive_attributes_passed':archive['passed'],'loaded_reference_scope':resolver.loaded,
               'before':frozen['before'],'after':after,'collision_fingerprint_unchanged':True,
               'agent_reference_permissions':guard,'deployment_recommended':False,
               'deployment_reason':'Proposals only. Independent sample evidence and historical uncertainty require separate review and explicit approval.'}
    save(output/'summary.json',summary)
    save(output/'run-manifest.json',{'snapshot_id':sid,'method_version':METHOD_VERSION,'created_at':now(),
         'files':{name:sha(output/name) for name in ('input.json','pair-results.json','candidates.jsonl','candidates.csv','summary.json','stage-a-replay.json','rest-archive-comparison.json')}})
    return summary
