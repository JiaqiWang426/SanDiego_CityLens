"""Stratified TIGER transportation review, distinct from SanGIS connectivity.

This is an independent spatial diagnostic, not historical/grade ground truth.
Even a passing sample cannot by itself authorize collision updates.
"""
from collections import Counter, defaultdict
from pathlib import Path
import re

from backend.verification.intersection_feasibility.study import independent_check, connection
from .acquire import Capture, EvidenceError, digest, now, read, save, sha


class IndependentCapture(Capture):
    def query(self,endpoint,params,kind):
        payload=super().query(endpoint,params,kind)
        for feature in payload.get('results',[]):
            if feature.get('geometryType')=='esriGeometryPolyline':
                sr=(feature.get('geometry') or {}).get('spatialReference') or {}
                if sr.get('latestWkid',sr.get('wkid'))!=4326:
                    raise EvidenceError('Independent transportation geometry CRS is unconfirmed')
        return payload


def select(results, attributes, target_accepted=60):
    accepted = [r for r in results if r['status']=='accepted_candidate']
    strata = defaultdict(list)
    for r in accepted:
        ids = {i for c in r['candidates'] for i in c['incident_segment_ids']}
        classes = {attributes[i]['segclass'] for i in ids}
        high_risk = bool(classes & {'1','2','7','8','9','A','M','Z'})
        directions = bool(re.search(r'\b(N|S|E|W|NE|NW|SE|SW)\b',r['primary']+' '+r['cross']))
        complex_name = max(len(r['primary'].split()),len(r['cross'].split()))>=4
        strata[(high_risk,directions,complex_name)].append(r)
    queues = [sorted(group,key=lambda r:digest(r['pair_id'])) for key,group in sorted(strata.items(),reverse=True)]
    selected = []
    while queues and len(selected)<target_accepted:
        for queue in list(queues):
            if len(selected)>=target_accepted:
                break
            selected.append(queue.pop(0))
            if not queue:
                queues.remove(queue)
    # Include supported and unsupported hard cases. All nodes of an ambiguous
    # pair are retained, never just the first (may use several REST requests).
    hard_groups = defaultdict(list)
    for r in results:
        if r['status']!='accepted_candidate' and any(c['longitude'] is not None for c in r['candidates']):
            kind = 'grade_conflict' if r['grade_separated_conflicts'] else r['status']
            hard_groups[kind].append(r)
    for kind,group in sorted(hard_groups.items()):
        quota = 12 if kind=='grade_conflict' else 10
        selected.extend(sorted(group,key=lambda r:digest(r['pair_id']))[:quota])
    if sum(sum(c['longitude'] is not None for c in r['candidates']) for r in selected)>170:
        raise EvidenceError('Frozen independent sample exceeds bounded request budget')
    return selected


def independent_review(output, *, live=False):
    output = Path(output)
    run = read(output/'run-manifest.json')
    for name in ('pair-results.json','candidates.jsonl'):
        if sha(output/name)!=run['files'][name]:
            raise EvidenceError('Dry run changed before independent review')
    results = read(output/'pair-results.json')
    with connection() as conn:
        attributes = {r['roadsegid']:r for r in conn.execute('SELECT roadsegid,segclass,segstat FROM citylens_ref.road_segments WHERE snapshot_id=%s',(run['snapshot_id'],))}
    plan_path = output/'independent-plan.json'
    if plan_path.exists():
        plan = read(plan_path)
        if plan['candidate_sha256']!=sha(output/'candidates.jsonl'):
            raise EvidenceError('Frozen independent sample belongs to different candidates')
        by_id={r['pair_id']:r for r in results}
        if any(pair_id not in by_id for pair_id in plan['pair_ids']):
            raise EvidenceError('Frozen independent plan contains missing pairs')
        selected = [by_id[pair_id] for pair_id in plan['pair_ids']]
    else:
        selected = select(results,attributes)
        plan = {'created_at':now(),'snapshot_id':run['snapshot_id'],'candidate_sha256':sha(output/'candidates.jsonl'),
                'pair_ids':[r['pair_id'] for r in selected],
                'selection':'Deterministic round-robin accepted strata: high-risk class, directions, name complexity; plus grade conflicts, multiple nodes, uncertain nodes. Not a random population sample.'}
        save(plan_path,plan)
    capture = IndependentCapture(output/'independent-evidence', cap=180, live=live)
    checks = []
    for index,result in enumerate(selected):
        check = independent_check(result,capture)
        node_statuses={c['node_id']:c['status'] for c in result['candidates']}
        for node_check in check['checks']:
            node_check['topology_outcome']=node_statuses[node_check['node_id']]
        checks.append(check)
        save(output/'independent-validation.json',checks)
        if index%10==0:
            print(f'Independent spatial review {index+1}/{len(selected)} pairs',flush=True)
    flat = [c for r in checks for c in r['checks']]
    accepted = [c for r in checks if r['study_status']=='accepted_candidate' for c in r['checks'] if c['topology_outcome']=='supported']
    high_risk = Counter()
    for result in selected:
        for candidate in result['candidates']:
            for i in candidate['incident_segment_ids']:
                high_risk[attributes[i]['segclass']] += 1
    result = {'checked_at':now(),'pairs':len(checks),'candidate_checks':len(flat),
              'study_classifications':dict(Counter(r['study_status'] for r in checks)),
              'candidate_outcomes':dict(Counter(c['status'] for c in flat)),
              'accepted_candidate_checks':len(accepted),'accepted_outcomes':dict(Counter(c['status'] for c in accepted)),
              'incident_class_occurrences':dict(high_risk),
              'accepted_uncorroborated_pairs':[{'pair_id':r['pair_id'],'primary':r['primary'],'cross':r['cross'],'checks':r['checks']} for r in checks
                  if r['study_status']=='accepted_candidate' and any(c['topology_outcome']=='supported' and c['status']!='both_roads_spatially_corroborated' for c in r['checks'])],
              'complete':all(c['status']!='provider_error' for c in flat),
              'systematic_error_discovered':False,
              'false_positive_rate':None,'safe_deployment_established':False,
              'limitation':'Exact-name independent spatial proximity is not physical connectivity, surveyed accuracy, or historical ground truth. A stratified nonrandom sample cannot supply a population false-positive rate. Uncorroborated names require review; corroborated grade-separated crossings illustrate the limitation.',
              'new_source':'US Census TIGERweb Transportation identify; NOT Census address geocoding or a repeat of the Census batch.',
              'direct_requests':len(capture.ledger['attempts']),
              'files':{'independent-plan.json':sha(plan_path),'independent-validation.json':sha(output/'independent-validation.json'),
                       'requests.json':sha(output/'independent-evidence'/'requests.json')}}
    save(output/'independent-summary.json',result)
    from .inspection import inspect
    inspect(output)
    return result
