"""Opt-in Google v3 evaluation adapter. Never writes results or logs request URLs.

Not installed in the production provider chain. Real requests require an explicit
live authorization in addition to a key; every attempt consumes the shared cap.
"""
from collections import Counter,deque
from dataclasses import dataclass,field
import math
import os
import threading
import time
from urllib.parse import urlencode
from uuid import uuid4

from ..config import load_environment
from .geocoding_http import get_json,GeocodingRequestError
from .location_models import GeocodedCandidate
from .models import Location
from .user_geocoding import AddressResolution,interactive_street

PROVIDER='Google Maps'
ENDPOINT='https://maps.googleapis.com/maps/api/geocode/json'
PRECISIONS={'ROOFTOP':'rooftop','RANGE_INTERPOLATED':'range_interpolated',
            'GEOMETRIC_CENTER':'geometric_center','APPROXIMATE':'approximate'}
PRICE_PER_REQUEST_USD=.005  # Official base-tier list price checked 2026-11-02.


@dataclass
class GoogleRequestBudget:
    """Process-local experimental budget, not a substitute for Cloud quotas."""
    max_requests: int=15
    per_minute: int=15
    requests: int=0
    blocked_reason: str | None=None
    clock: object=time.monotonic
    _times: deque=field(default_factory=deque,repr=False)
    _lock: object=field(default_factory=threading.Lock,repr=False)

    def __post_init__(self):
        if not 1<=self.max_requests<=15 or not 1<=self.per_minute<=60:
            raise ValueError('Google experiment requires a cap of 1..15 and rate of 1..60/minute')

    def acquire(self):
        with self._lock:
            if self.blocked_reason:return self.blocked_reason
            if self.requests>=self.max_requests:return 'google_request_cap_exhausted'
            now=self.clock()
            while self._times and self._times[0]<=now-60:self._times.popleft()
            if len(self._times)>=self.per_minute:return 'google_rate_limited'
            self._times.append(now);self.requests+=1
            return None

    def block(self,reason):
        with self._lock:self.blocked_reason=reason

    def snapshot(self):
        with self._lock:
            return dict(requests=self.requests,cap=self.max_requests,
                estimated_list_cost_usd=round(self.requests*PRICE_PER_REQUEST_USD,6),
                maximum_list_cost_usd=round(self.max_requests*PRICE_PER_REQUEST_USD,6),
                blocked_reason=self.blocked_reason,scope='this process/budget instance')


class GoogleEvaluationGeocoder:
    def __init__(self,api_key=None,*,live_authorized=False,budget=None,transport=None,attempts=1):
        if attempts not in {1,2}:raise ValueError('Use at most two Google attempts')
        self.api_key=api_key
        self.live_authorized=live_authorized
        self.budget=budget or GoogleRequestBudget()
        self.transport=transport or get_json
        self.attempts=attempts

    @classmethod
    def from_env(cls,*,live_authorized=False,budget=None):
        load_environment()
        return cls(os.getenv('GOOGLE_MAPS_API_KEY'),live_authorized=live_authorized,budget=budget)

    def failure(self,reason,**extra):
        return AddressResolution('provider_error',[],dict(provider=PROVIDER,reason=reason,
            usage=self.budget.snapshot(),**extra))

    def resolve(self,address):
        if not self.live_authorized:return self.failure('google_live_not_authorized')
        if not self.api_key:return self.failure('google_key_missing')
        if not address.house_number or not address.house_number.isdigit() or not address.street_name:
            return AddressResolution('unsupported',[],dict(provider=PROVIDER,reason='numbered_street_required'))
        params=dict(address=address.display,components='country:US|administrative_area:CA',key=self.api_key)
        for attempt in range(self.attempts):
            denied=self.budget.acquire()
            if denied:return self.failure(denied)
            try:
                # One HTTP attempt per acquired budget token; helper cannot retry.
                payload,diagnostics=self.transport(ENDPOINT+'?'+urlencode(params),timeout=15,attempts=1,follow_redirects=False)
            except GeocodingRequestError as exc:
                errors=exc.metadata.get('request_errors',[])
                codes=[e.get('http_status') for e in errors]
                if 429 in codes:self.budget.block('google_quota_exhausted')
                transient=bool(errors) and all(e.get('http_status') in {None,408,500,502,503,504} for e in errors)
                if transient and attempt+1<self.attempts:
                    time.sleep(.5);continue
                return self.failure('google_quota_exhausted' if 429 in codes else 'google_http_error',
                    request_errors=errors)
            # Do not retain error_message, request URL, response body or credentials.
            status=payload.get('status') if isinstance(payload,dict) and isinstance(payload.get('status'),str) else None
            if status in {'OVER_QUERY_LIMIT','OVER_DAILY_LIMIT'}:
                self.budget.block('google_quota_exhausted' if status=='OVER_QUERY_LIMIT' else 'google_daily_limit_or_billing_error')
            if status=='REQUEST_DENIED':self.budget.block('google_request_denied')
            if status=='UNKNOWN_ERROR' and attempt+1<self.attempts:
                time.sleep(.5);continue
            result=self.parse_response(address,payload)
            result.metadata.update(usage=self.budget.snapshot(),latency_ms=diagnostics.get('latency_ms'))
            return result

    def parse_response(self,address,payload):
        """Pure parser. Caller must keep any real Google content transient."""
        info=dict(provider=PROVIDER,raw_candidate_count=0,rejections={})
        if not isinstance(payload,dict):return AddressResolution('invalid_or_out_of_scope',[],dict(info,reason='malformed_response'))
        status=payload.get('status')
        if not isinstance(status,str):return AddressResolution('invalid_or_out_of_scope',[],dict(info,reason='malformed_status'))
        if status=='ZERO_RESULTS' and payload.get('results')==[]:
            return AddressResolution('no_match',[],info)
        if status!='OK':
            reason='google_quota_exhausted' if status=='OVER_QUERY_LIMIT' else 'google_daily_limit_or_billing_error' if status=='OVER_DAILY_LIMIT' else 'google_'+str(status).lower() if status in {'REQUEST_DENIED','INVALID_REQUEST','UNKNOWN_ERROR'} else 'google_malformed_status'
            return AddressResolution('provider_error',[],dict(info,reason=reason,
                provider_status=status if status in {'OVER_QUERY_LIMIT','OVER_DAILY_LIMIT','REQUEST_DENIED','INVALID_REQUEST','UNKNOWN_ERROR'} else 'malformed'))
        results=payload.get('results')
        if not isinstance(results,list) or not results:
            return AddressResolution('invalid_or_out_of_scope',[],dict(info,reason='malformed_results'))
        info['raw_candidate_count']=len(results)
        rejected=Counter();candidates=[];seen=set()
        for result in results:
            try:
                if not isinstance(result,dict):raise ValueError('malformed_result')
                components=result.get('address_components')
                if not isinstance(components,list):raise ValueError('missing_components')
                parts={}
                for component in components:
                    if not isinstance(component,dict) or not isinstance(component.get('types'),list):raise ValueError('malformed_components')
                    for kind in component['types']:
                        if kind in parts and parts[kind]!=component:raise ValueError('conflicting_components')
                        parts[kind]=component
                def value(kind,short=False):
                    item=parts.get(kind,{})
                    v=item.get('short_name' if short else 'long_name')
                    return v.strip() if isinstance(v,str) else ''
                if value('street_number')!=address.house_number:raise ValueError('house_number')
                if interactive_street(value('route'))!=interactive_street(address.street):raise ValueError('street_components')
                if value('country',True)!='US' or value('administrative_area_level_1',True)!='CA':raise ValueError('country_or_state')
                city=value('locality').upper()
                # Only the already-supported San Diego / La Jolla equivalence.
                if city not in {'SAN DIEGO','LA JOLLA'}:raise ValueError('locality')
                if (address.city or 'San Diego').upper() not in {'SAN DIEGO','LA JOLLA'}:raise ValueError('input_locality')
                if address.postal_code and value('postal_code')!=address.postal_code[:5]:raise ValueError('postal_code')
                geometry=result.get('geometry')
                if not isinstance(geometry,dict):raise ValueError('missing_geometry')
                point=geometry.get('location')
                if not isinstance(point,dict):raise ValueError('missing_geometry')
                lat,lng=point.get('lat'),point.get('lng')
                if any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) for v in (lat,lng)):
                    raise ValueError('invalid_geometry')
                location=Location(id=address.id,latitude=lat,longitude=lng)
                precision=PRECISIONS.get(geometry.get('location_type'))
                if not precision:raise ValueError('unknown_precision')
                formatted=result.get('formatted_address');place_id=result.get('place_id')
                if not isinstance(formatted,str) or not formatted.strip() or not isinstance(place_id,str) or not place_id:
                    raise ValueError('missing_address_or_place_id')
                partial=result.get('partial_match',False)
                if not isinstance(partial,bool):raise ValueError('malformed_partial_match')
                unit=value('subpremise')
                key=(place_id,lat,lng,partial,precision,unit)
                if key in seen:continue
                seen.add(key)
                candidates.append(GeocodedCandidate(candidate_id='candidate_'+uuid4().hex,
                    matched_address=formatted,location=location.model_copy(update={'label':formatted}),
                    provider=PROVIDER,precision=precision,requires_confirmation=partial or precision!='rooftop' or bool(unit),
                    mismatch_reason='partial_match' if partial else None,
                    metadata=dict(location_type=geometry['location_type'],partial_match=partial,place_id=place_id,
                        address_components=components,srid=4326,attribution='Google Maps',
                        retention='transient evaluation only; never commit or persist coordinates',
                        precision_basis='Provider address-level estimate; not independent proof of building or entrance')))
            except (ValueError,TypeError,KeyError) as exc:
                reason=str(exc) if type(exc) is ValueError and str(exc) in {
                    'malformed_result','missing_components','malformed_components','conflicting_components',
                    'house_number','street_components','country_or_state','locality','input_locality','postal_code',
                    'missing_geometry','invalid_geometry','unknown_precision','missing_address_or_place_id',
                    'malformed_partial_match'} else 'malformed_or_out_of_scope'
                rejected[reason]+=1
        info['rejections']=dict(rejected);info['distinct_candidate_count']=len(candidates)
        if not candidates:return AddressResolution('invalid_or_out_of_scope',[],info)
        if len(candidates)>1:
            for candidate in candidates:candidate.requires_confirmation=True
        return AddressResolution('resolved' if len(candidates)==1 and not candidates[0].requires_confirmation else 'needs_confirmation',candidates,info)
