import argparse
import json
from pathlib import Path

from .acquire import acquire, DEFAULT_DATA, EvidenceError, read
from .batch import dry_run, DEFAULT_OUTPUT
from .database import import_snapshot, validate_snapshot
from .verify import independent_review


def main():
    parser = argparse.ArgumentParser(description='SanGIS reference infrastructure and historical intersection proposals; default is dry run')
    parser.add_argument('command', choices=['download','import','validate','dry-run','verify','stage-c','stage-c-preflight','deployment','final-geocoding','final-deployment'])
    parser.add_argument('--data',type=Path,default=DEFAULT_DATA)
    parser.add_argument('--output',type=Path,default=DEFAULT_OUTPUT)
    parser.add_argument('--live',action='store_true',help='Permit capped independent TIGER transportation requests')
    parser.add_argument('--apply',action='store_true',help='Deployment only; requires separate exact-artifact approval and maintenance credentials')
    parser.add_argument('--approval',type=Path)
    parser.add_argument('--batch-size',type=int,default=100)
    parser.add_argument('--stage-c',type=Path,default=None,help='Stage C validation directory (allowlist, manifests)')
    parser.add_argument('--cache',type=Path,default=None,help='Stage C ignored TIGERweb cache directory')
    parser.add_argument('--budget',type=int,default=None,help='Stage C live TIGERweb attempt budget (persisted)')
    args = parser.parse_args()
    if args.apply and args.command not in {'deployment','final-deployment'}:
        parser.error('--apply is only valid with deployment')
    if args.command=='download':
        result = acquire(args.data)
        print(json.dumps({'snapshot_id':result['snapshot_id'],'counts':{k:v['feature_count'] for k,v in result['layers'].items()}}))
        return
    if args.command=='import':
        result = import_snapshot(args.data)
    elif args.command=='validate':
        sid = read(args.data/'manifest.json')['snapshot_id']
        result = validate_snapshot(sid,args.output/'topology-integrity.json')
    elif args.command=='dry-run':
        sid = read(args.data/'manifest.json')['snapshot_id']
        result = dry_run(sid,args.output)
    elif args.command=='verify':
        result = independent_review(args.output,live=args.live)
    elif args.command=='stage-c':
        from . import stage_c
        result = stage_c.run(args.output,args.stage_c or stage_c.DEFAULT_STAGE_C,args.cache or stage_c.DEFAULT_CACHE,
                             live=args.live,budget=args.budget or stage_c.DEFAULT_BUDGET)
        stage_c.approval_template(args.stage_c or stage_c.DEFAULT_STAGE_C,args.output)
        stage_c.first_batch_proposal(args.stage_c or stage_c.DEFAULT_STAGE_C)
    elif args.command=='final-geocoding':
        from . import final_geocoding
        result = final_geocoding.run(live=args.live,budget=args.budget or final_geocoding.DEFAULT_BUDGET)
        final_geocoding.approval_template()
        final_geocoding.first_batch_proposal()
        final_geocoding.rollout_plan()
    elif args.command=='final-deployment':
        if not args.approval:
            parser.error('Final deployment preflight and apply require --approval from a reviewer')
        from .apply import deploy_final
        from .final_geocoding import DEFAULT_OUTPUT as FINAL
        result = deploy_final(FINAL,args.approval,apply=args.apply,batch_size=args.batch_size)
    elif args.command=='stage-c-preflight':
        from . import stage_c
        result = stage_c.allowlist_preflight(args.stage_c or stage_c.DEFAULT_STAGE_C)
    else:
        if not args.approval:
            parser.error('Deployment preflight and apply require --approval from a reviewer')
        from .apply import deploy
        from .stage_c import DEFAULT_STAGE_C
        from .acquire import read as read_json
        from .final_geocoding import DEFAULT_OUTPUT as FINAL
        snapshot = FINAL/'before-row-digests.json.gz'
        snapshot_sha = read_json(FINAL/'validation-manifest.json')['files']['before-row-digests.json.gz'] if snapshot.exists() else None
        # The per-row baseline lets Stage C batches interleave with final-allowlist batches.
        result = deploy(args.output,args.approval,apply=args.apply,batch_size=args.batch_size,stage_c=args.stage_c or DEFAULT_STAGE_C,
                        baseline_snapshot=snapshot if snapshot.exists() else None,baseline_sha256=snapshot_sha)
    print(json.dumps({k:v for k,v in result.items() if k not in {'before','after','comparisons','accepted_uncorroborated_pairs','distribution'}},default=str))


if __name__=='__main__':
    try:
        main()
    except Exception as exc:
        # libpq errors may contain credentials. Never print arbitrary exception text.
        if isinstance(exc,EvidenceError):
            raise SystemExit(str(exc)) from None
        raise SystemExit('Stage B stopped: '+type(exc).__name__+'; credentials and database exception details withheld') from None
