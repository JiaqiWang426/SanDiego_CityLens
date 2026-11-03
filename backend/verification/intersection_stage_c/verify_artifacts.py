"""Offline consistency check of the Stage C deliverables. No database, no network.

    .\\.venv\\Scripts\\python.exe -m backend.verification.intersection_stage_c.verify_artifacts

Exits nonzero on any mismatch. It does not re-run validation (use
`python -m backend.sangis_topology stage-c`, which replays cached evidence).
"""
import csv
import json
from pathlib import Path
import sys

from backend.sangis_topology.acquire import read, sha
from backend.sangis_topology.stage_c import VALIDATION_VERSION, read_allowlist

HERE = Path(__file__).resolve().parent
STAGE_B = HERE.parent / 'intersection_batch'


def rows(name):
    with (HERE / name).open(encoding='utf-8', newline='') as stream:
        return list(csv.DictReader(stream))


def main():
    problems = []
    check = lambda ok, message: ok or problems.append(message)
    manifest = read(HERE / 'validation-manifest.json')
    for name, checksum in manifest['files'].items():
        check(sha(HERE / name) == checksum, 'checksum mismatch: ' + name)
    check(manifest['validation_version'] == VALIDATION_VERSION, 'validation version')
    allow_sha = (HERE / 'deployment_allowlist.sha256').read_text(encoding='utf-8').split()[0]
    check(allow_sha == sha(HERE / 'deployment_allowlist.csv'), 'allowlist checksum file')
    summary = read(HERE / 'summary.json')
    results, excluded, allow = rows('validation_results.csv'), rows('excluded_candidates.csv'), read_allowlist(HERE / 'deployment_allowlist.csv')
    ids = [r['report_id'] for r in results]
    check(len(ids) == len(set(ids)) == summary['stage_b']['accepted_reports'], 'every Stage B accepted report has exactly one outcome')
    check(set(allow) | {r['report_id'] for r in excluded} == set(ids) and not set(allow) & {r['report_id'] for r in excluded}, 'allowlist/excluded partition')
    check(all(r['validation_status'] == 'pass' for r in allow.values()), 'allowlist contains only pass')
    check(all(r['validation_status'] != 'pass' and json.loads(r['exclusion_reasons']) for r in excluded), 'every exclusion keeps a reason')
    check(all(r['precision'] == 'intersection_approximate' for r in allow.values()), 'precision')
    check(len({r['pair_id'] for r in results}) == summary['stage_b']['accepted_pairs'] == summary['pairs']['checked'], 'every accepted pair checked')
    for status in ('pass', 'fail', 'unknown', 'provider_error'):
        check(sum(r['validation_status'] == status for r in results) == summary['reports'][status], 'report count ' + status)
    stage_b = {}
    with (STAGE_B / 'candidates.jsonl').open(encoding='utf-8') as stream:
        for line in stream:
            c = json.loads(line)
            if c['classification'] == 'accepted_candidate' and c['topology_outcome'] == 'supported':
                stage_b[c['report_id']] = c
    for report_id, row in allow.items():
        c = stage_b.get(report_id)
        check(c is not None and c['node_id'] == row['sangis_node_id'] and c['longitude'] == row['longitude']
              and c['latitude'] == row['latitude'] and c['source_address_fingerprint'] == row['source_address_fingerprint'],
              'allowlist row differs from Stage B proposal: ' + report_id)
    check(summary['collision_updates'] == 0 and summary['coverage']['deployed_by_stage_c'] == 0, 'no deployment claimed')
    review = read(HERE / 'quality-review.json')
    check(review['quality_sample_sha256'] == sha(HERE / 'quality-sample.json'), 'quality review bound to sample')
    template = read(HERE / 'approval-template.json')
    check(template['decision'] != 'approve_apply' and not template['report_ids'], 'template is not an approval')
    print(json.dumps({'checked_reports': len(ids), 'allowlist_reports': len(allow), 'excluded_reports': len(excluded),
                      'problems': problems}, indent=2))
    return 1 if problems else 0


if __name__ == '__main__':
    sys.exit(main())
