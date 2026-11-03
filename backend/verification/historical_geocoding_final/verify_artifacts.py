"""Offline consistency check of the final historical geocoding deliverables.

    .\\.venv\\Scripts\\python.exe -m backend.verification.historical_geocoding_final.verify_artifacts

No database, no network. Exits nonzero on any mismatch.
"""
import csv
import json
from collections import Counter
from pathlib import Path
import sys

from backend.sangis_topology.acquire import read, sha
from backend.sangis_topology.final_geocoding import METHOD_VERSION
from backend.sangis_topology.rollout import load_snapshot

HERE = Path(__file__).resolve().parent
STAGE_C = HERE.parent / 'intersection_stage_c'


def rows(path):
    with Path(path).open(encoding='utf-8', newline='') as stream:
        return list(csv.DictReader(stream))


def main():
    problems = []
    check = lambda ok, message: ok or problems.append(message)
    manifest = read(HERE / 'validation-manifest.json')
    check(manifest['method_version'] == METHOD_VERSION, 'method version')
    for name, checksum in manifest['files'].items():
        check(sha(HERE / name) == checksum, 'checksum mismatch: ' + name)
    check((HERE / 'deployment_allowlist.sha256').read_text(encoding='utf-8').split()[0] == sha(HERE / 'deployment_allowlist.csv'), 'allowlist checksum file')
    summary = read(HERE / 'summary.json')
    results, accepted, excluded, allow = (rows(HERE / n) for n in ('geocoding_results.csv', 'accepted_candidates.csv', 'excluded_candidates.csv', 'deployment_allowlist.csv'))
    ids = [r['report_id'] for r in results]
    check(len(ids) == len(set(ids)), 'one outcome per report')
    check(Counter(r['original_status'] for r in results) == Counter({k: v for k, v in summary['baseline_statuses'].items() if k != 'matched'}),
          'every no_match/ambiguous/pending report evaluated exactly once')
    check({r['report_id'] for r in accepted} | {r['report_id'] for r in excluded} == set(ids)
          and not {r['report_id'] for r in accepted} & {r['report_id'] for r in excluded}, 'accepted/excluded partition')
    check({r['report_id'] for r in allow} == {r['report_id'] for r in results if r['deployment_route'] == 'final_allowlist'}, 'allowlist = final route')
    check(all(r['validation_status'] == 'pass' and r['deployable'] == 'True' for r in results if r['deployment_route'] != 'none'), 'routes only for pass')
    check(all(r['deployable'] == 'False' for r in excluded) and all(json.loads(r['exclusion_reasons']) or r['outcome'] for r in excluded), 'exclusions keep reasons')
    check(all(r['precision'] in ('block_approximate', 'intersection_approximate') for r in allow), 'approximate precision')
    check(all(r['original_status'] in ('no_match', 'ambiguous', 'pending') for r in allow), 'never a matched row')
    stage_c = {r['report_id'] for r in rows(STAGE_C / 'deployment_allowlist.csv')}
    check(stage_c == {r['report_id'] for r in results if r['deployment_route'] == 'stage_c_allowlist'}, 'Stage C allowlist routed unchanged')
    check(not stage_c & {r['report_id'] for r in allow}, 'no report in both allowlists')
    snapshot = load_snapshot(HERE / 'before-row-digests.json.gz', manifest['files']['before-row-digests.json.gz'])
    check(snapshot['collision_audit'] == read(HERE / 'before-state.json')['collision_audit'], 'row baseline matches before-state')
    check(summary['collision_updates'] == 0 and summary['coverage']['deployed_by_this_milestone'] == 0, 'no deployment claimed')
    review = read(HERE / 'quality-review.json')
    check(review['quality_sample_sha256'] == sha(HERE / 'quality-sample.json'), 'quality review bound to sample')
    template = read(HERE / 'approval-template.json')
    check(template['decision'] != 'approve_apply' and not template['report_ids'], 'template is not an approval')
    print(json.dumps({'evaluated': len(ids), 'final_allowlist': len(allow), 'stage_c_routed': len(stage_c), 'problems': problems}, indent=2))
    return 1 if problems else 0


if __name__ == '__main__':
    sys.exit(main())
