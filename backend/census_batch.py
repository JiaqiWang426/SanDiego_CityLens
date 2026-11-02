"""Official Census locations/addressbatch CSV transport and strict ID mapping.

No single-record JSON fields are assumed. Raw eight-column matched rows are
retained; batch CSV has no address components/ranges or returned benchmark.
"""
import csv
import io
import json
import re
import time
import uuid
from urllib.error import HTTPError,URLError
from urllib.request import Request,urlopen

if __package__:
    from .census_addresses import match_reason
    from .collision_addresses import clean_text
    from .collision_geocoding import GeocodeResult
else:
    from census_addresses import match_reason
    from collision_addresses import clean_text
    from collision_geocoding import GeocodeResult

ENDPOINT='https://geocoding.geo.census.gov/geocoder/locations/addressbatch'
PROVIDER='US Census Geocoder Batch'
MAX_ADDRESSES=10000
MAX_FILE_BYTES=5_000_000


class BatchProtocolError(ValueError):
    """Untrustworthy batch mapping/format: stop without applying the batch."""


class ProviderUnavailable(RuntimeError):
    """Retries exhausted or permanent HTTP error; current records stay pending."""


def input_csv(requests):
    if not 1<=len(requests)<=MAX_ADDRESSES:
        raise ValueError('Batch must contain 1..10000 unique addresses')
    stream=io.StringIO(newline='')
    writer=csv.writer(stream,lineterminator='\r\n')
    for id,address in requests.items():
        if not id or not address['street']:
            raise ValueError('Unique ID and street are required')
        writer.writerow([id,address['street'],'San Diego','CA',''])
    data=stream.getvalue().encode('utf-8')
    if len(data)>MAX_FILE_BYTES:
        raise ValueError('CSV exceeds conservative 5 MB limit')
    return data


def _echo(value):
    return re.sub(r'\s*,\s*',',',clean_text(value)).strip(' ,')


def parse_batch(text,requests,benchmark='Public_AR_Current',batch_id=None):
    """Validate IDs AND echoed inputs before any caller may write results.

    Unknown, duplicate, missing IDs, unexpected flags and malformed CSV stop
    the entire batch. An ordinary Tie or No_Match never invents a point.
    Invalid/Error provider rows become error results, with raw diagnostics.
    """
    parsed={}
    try:
        rows=list(csv.reader(io.StringIO(text,newline=''),strict=True))
    except csv.Error as exc:
        raise BatchProtocolError('Malformed CSV') from exc
    for row in rows:
        if not row or not any(row):
            continue
        id=row[0].lstrip('\ufeff').strip()
        if id not in requests or id in parsed:
            raise BatchProtocolError('Unknown or duplicate output identifier')
        if len(row)<3 or len(row)>8:
            raise BatchProtocolError('Unexpected batch output column count')
        address=requests[id]
        if _echo(row[1])!=_echo(address['street']+', San Diego, CA,'):
            raise BatchProtocolError('Identifier maps to a different echoed input address')
        flag=re.sub(r'[ _-]','',row[2]).lower()
        evidence=dict(benchmark_requested=benchmark,benchmark_returned=None,batch_id=batch_id,
            request_id=id,submitted_street=address['street'],raw_row=row,
            provider_status=row[2],address_range_available=False,
            validation_policy='basic_address_consistency_v1',
            limitation='Interpolated anonymized block; not an exact accident location')
        if flag in {'nomatch','tie','invalid','invalidaddress','error','failed'}:
            status='no_match' if flag=='nomatch' else 'ambiguous' if flag=='tie' else 'error'
            evidence['reason']='provider_'+flag
            parsed[id]=GeocodeResult(status,PROVIDER,details=evidence)
            continue
        if flag!='match' or len(row)!=8:
            raise BatchProtocolError('Unknown match indicator or incomplete matched row')
        if row[3].strip().lower() not in {'exact','non_exact','non-exact','equivocate'}:
            raise BatchProtocolError('Unknown match type')
        evidence.update(match_type=row[3],matched_address=row[4],tiger_line_id=row[6],side=row[7])
        parts=[part.strip() for part in row[4].split(',')]
        numbered=re.fullmatch(r'([0-9]+)\s+(.+)',parts[0]) if parts else None
        coordinates=row[5].split(',')
        if len(parts)!=4 or not numbered or len(coordinates)!=2:
            raise BatchProtocolError('Malformed matched address or coordinate pair')
        try:
            longitude,latitude=map(float,coordinates)
        except ValueError as exc:
            raise BatchProtocolError('Nonnumeric matched coordinates') from exc
        reason=match_reason(numbered[1],numbered[2],parts[1],parts[2],longitude,latitude,
                            address['number'],address['primary_street'])
        if reason:
            evidence.update(reason=reason,validation_rejected=True)
            parsed[id]=GeocodeResult('ambiguous',PROVIDER,details=evidence)
        else:
            parsed[id]=GeocodeResult('matched',PROVIDER,longitude,latitude,'block_approximate',evidence)
    if set(parsed)!=set(requests):
        raise BatchProtocolError('Missing output identifiers; partial batch retained for review')
    return parsed


def multipart(csv_bytes,benchmark):
    boundary='CityLens'+uuid.uuid4().hex
    chunks=[f'--{boundary}\r\nContent-Disposition: form-data; name="benchmark"\r\n\r\n{benchmark}\r\n'.encode(),
        f'--{boundary}\r\nContent-Disposition: form-data; name="addressFile"; filename="addresses.csv"\r\nContent-Type: text/csv\r\n\r\n'.encode(),
        csv_bytes,f'\r\n--{boundary}--\r\n'.encode()]
    return b''.join(chunks),boundary


class CensusBatchClient:
    def __init__(self,benchmark='Public_AR_Current',timeout=180,attempts=3,backoff=5):
        if attempts not in range(1,4) or timeout<=0 or not 0<=backoff<=30:
            raise ValueError('Use 1..3 attempts, positive timeout, backoff 0..30 seconds')
        self.benchmark,self.timeout,self.attempts,self.backoff=benchmark,timeout,attempts,backoff

    def submit(self,requests,*,event=lambda value:None):
        data=input_csv(requests)
        body,boundary=multipart(data,self.benchmark)
        for attempt in range(1,self.attempts+1):
            start=time.monotonic()
            entry=dict(attempt=attempt,addresses=len(requests),input_bytes=len(data))
            try:
                request=Request(ENDPOINT,data=body,headers={
                    'Content-Type':'multipart/form-data; boundary='+boundary,
                    'User-Agent':'CityLens/0.2 (public-data research)'})
                with urlopen(request,timeout=self.timeout) as response:
                    output=response.read(20_000_001)
                if len(output)>20_000_000:
                    raise BatchProtocolError('Response exceeds bounded size')
                text=output.decode('utf-8-sig')
                entry.update(outcome='response',seconds=round(time.monotonic()-start,3),response_bytes=len(output))
                event(entry)
                return text
            except (HTTPError,URLError,TimeoutError,OSError) as exc:
                code=exc.code if isinstance(exc,HTTPError) else None
                transient=code in {408,429,500,502,503,504} or code is None
                entry.update(outcome='request_error',error_type=type(exc).__name__,http_status=code,
                             seconds=round(time.monotonic()-start,3))
                event(entry)
                if not transient or attempt==self.attempts:
                    raise ProviderUnavailable('Census batch request failed; pending records preserved') from None
                delay=min(60,self.backoff*2**(attempt-1))
                if isinstance(exc,HTTPError):
                    try: delay=max(delay,min(60,float(exc.headers.get('Retry-After',0))))
                    except (TypeError,ValueError): pass
                time.sleep(delay)
        raise AssertionError('Unreachable')
