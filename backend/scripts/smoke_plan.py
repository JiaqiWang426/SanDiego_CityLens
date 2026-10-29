"""Exactly one opt-in paid planner request; no tools, DB, composition or retries.

Run manually only after approving the paid call:
python -m backend.scripts.smoke_plan --approve-paid-call
"""
import argparse
import asyncio
import json
from pathlib import Path

from backend.agent.diagnostics import log_failure,model_failure_code
from backend.agent.llm import ModelNotConfigured,OpenAIModel
from backend.agent.models import Plan,QueryRequest
from backend.agent.settings import Settings
from backend.agent.workflow import validated_calls


async def smoke(settings: Settings) -> int:
    fixture=Path(__file__).resolve().parents[1]/'examples/agent/collision_count.request.json'
    request=QueryRequest.model_validate_json(fixture.read_text(encoding='utf-8'))
    stage='provider'
    try:
        raw=await asyncio.wait_for(OpenAIModel(settings).plan(request),settings.llm_timeout_seconds)
        stage='plan_validation'
        plan=Plan.model_validate(raw.model_dump())
        calls=validated_calls(request,plan,settings)
        print(json.dumps({'status':'plan_accepted','plan':plan.model_dump(mode='json'),
                          'planned_tools':[call.name for call in calls],
                          'database_tools_executed':0,'composition_calls':0}))
        return 0
    except Exception as exc:
        code='openai_not_configured' if isinstance(exc,ModelNotConfigured) else model_failure_code(exc,'planning',stage)
        log_failure(code,exc,stage=stage,secrets=(settings.openai_api_key,))
        print(json.dumps({'status':'error','code':code,'exception_type':type(exc).__name__,
                          'http_status':getattr(exc,'status_code',None)}))
        return 1


def main(argv: list[str] | None=None) -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--approve-paid-call',action='store_true',
                        help='Explicit approval for one billable planner request')
    args=parser.parse_args(argv)
    if not args.approve_paid_call:
        print('No API request made. Pass --approve-paid-call only after approving one paid call.')
        return 2
    return asyncio.run(smoke(Settings.from_env()))


if __name__=='__main__':
    raise SystemExit(main())
