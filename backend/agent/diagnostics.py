"""Failure classification and bounded diagnostics without credential/body dumps."""
from __future__ import annotations

import json
import logging
import os
import re

import openai
from pydantic import ValidationError

LOGGER = logging.getLogger('citylens.agent')


class StructuredOutputFailure(ValueError):
    def __init__(self, reason: str):
        self.reason=reason
        super().__init__(reason)


def model_failure_code(exc: Exception, phase: str, stage: str) -> str:
    """Distinguish provider transport, wire parsing, and internal plan rejection."""
    if isinstance(exc,(TimeoutError,openai.APITimeoutError)):
        return f'{phase}_timeout'
    if isinstance(exc,openai.APIStatusError):
        return f'{phase}_provider_error:{exc.status_code}'
    if isinstance(exc,openai.APIConnectionError):
        return f'{phase}_provider_connection_failed'
    if isinstance(exc,StructuredOutputFailure):
        return f'{phase}_{exc.reason}'
    if stage=='plan_validation':
        # These strings are fixed local validation reasons, never model text.
        if isinstance(exc,ValidationError):
            return 'plan_rejected:invalid_schema'
        if isinstance(exc,ValueError):
            reason=REJECTION_REASONS.get(str(exc),'invalid_plan')
            return f'plan_rejected:{reason}'
    if isinstance(exc,(ValidationError,json.JSONDecodeError)):
        return f'{phase}_structured_output_invalid'
    if isinstance(exc,openai.LengthFinishReasonError):
        return f'{phase}_output_incomplete:max_output_tokens'
    if isinstance(exc,openai.ContentFilterFinishReasonError):
        return f'{phase}_output_refused'
    if isinstance(exc,openai.APIError):
        return f'{phase}_provider_error'
    return f'{phase}_internal_error'


REJECTION_REASONS = {
    'Duplicate planned tools':'duplicate_tools',
    'Unsupported intent cannot execute data tools':'unsupported_intent_with_tools',
    'Comparison requires two pins':'comparison_requires_two_locations',
    'Trends require aggregation':'trend_requires_aggregation',
    'Drilldown requires bounded retrieval':'drilldown_requires_retrieval',
    'Request counts require the service counter':'request_count_requires_service_counter',
    'Two-pin collision plans require aggregation, not page sizes':'comparison_requires_aggregation',
    'An all-category count cannot substitute for unavailable service filters':'unsupported_service_filters',
    'Plan omitted an explicitly requested supported dimension':'requested_dimension_omitted',
    'Nondefault filters need an exact supporting question quote':'filter_quote_required',
    'Tool-call budget exceeded':'tool_budget_exceeded',
    'Empty plan without an unsupported reason':'empty_plan',
    'Composer changed verified claim set':'claim_set_changed',
}


def _redact(value: object, secrets: tuple[str | None,...]) -> str:
    """Scrub known credentials plus URL/key patterns; never log whole bodies."""
    text=str(value)
    for secret in (*secrets,os.getenv('OPENAI_API_KEY'),os.getenv('AGENT_DATABASE_URL'),os.getenv('DATABASE_URL')):
        if secret:
            text=text.replace(secret,'[redacted]')
    text=re.sub(r'\b(?:https?|postgres(?:ql)?)://[^\s\"\']+', '[redacted-url]',text)
    text=re.sub(r'\bsk-[A-Za-z0-9_-]+','[redacted-key]',text)
    text=re.sub(r'(?i)(password\s*[=:]\s*)[^\s,;]+',r'\1[redacted]',text)
    return ' '.join(text.split())[:400]


def log_failure(code: str,exc: Exception,*,stage: str,trace_id: str | None=None,
                query_id: str | None=None,secrets: tuple[str | None,...]=()) -> None:
    """No traceback, SQL values, questions, DSNs or arbitrary exception text.

    Provider message/param are bounded and scrubbed for diagnosis. Database
    failures log SQLSTATE/class only, since their messages may contain secrets.
    """
    fields={'code':code,'stage':stage,'exception_type':type(exc).__name__,
            'trace_id':trace_id,'query_id':query_id}
    sqlstate=getattr(exc,'sqlstate',None)
    if sqlstate:
        fields['sqlstate']=sqlstate
    if isinstance(exc,openai.APIStatusError):
        fields['http_status']=exc.status_code
        body=exc.body if isinstance(exc.body,dict) else {}
        error=body.get('error',body)
        if isinstance(error,dict):
            for key in ('message','param','code','type'):
                if error.get(key) is not None:
                    fields['provider_'+key]=_redact(error[key],secrets)
    if isinstance(exc,StructuredOutputFailure):
        fields['structured_output_reason']=exc.reason
    if isinstance(exc,ValueError) and str(exc) in REJECTION_REASONS:
        fields['validation_reason']=REJECTION_REASONS[str(exc)]
    LOGGER.warning('agent_failure %s',json.dumps(fields,sort_keys=True))
