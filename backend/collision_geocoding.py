"""Opt-in approximate geocoding; no guessed coordinates or automatic bulk calls.

Census interpolates structure addresses along TIGER address ranges. Collision
numbers are anonymized to blocks, so even a unique match is not an exact crash
location. This adapter cannot resolve intersections: it leaves them unsupported
instead of discarding the cross street. A local GIS adapter can implement the
same Provider protocol later, using verified road geometry/address ranges.
"""
import argparse
from dataclasses import asdict, dataclass
import json
import math
import os
import time
from typing import Protocol
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import psycopg
from psycopg.types.json import Jsonb

if __package__:
    from .collision_addresses import CollisionAddress, normalize_collision_address, normalize_street_name
    from .census_addresses import census_street,match_reason
else:
    from collision_addresses import CollisionAddress, normalize_collision_address, normalize_street_name
    from census_addresses import census_street,match_reason


@dataclass(frozen=True)
class GeocodeResult:
    status: str
    provider: str
    longitude: float | None = None
    latitude: float | None = None
    precision: str | None = None
    details: dict | None = None


class Provider(Protocol):
    """Adapters must preserve evidence and return no coordinates on ambiguity."""
    def geocode(self, address: CollisionAddress) -> GeocodeResult: ...


class CensusGeocoder:
    """Census structured-address API, with configurable timeout/benchmark.

    Uses only the primary block address and only when no cross street exists.
    Rejects multiple matches, unexpected street/number/state, and invalid output.
    Does not convert a provider result into an exact-address claim.
    """
    endpoint = 'https://geocoding.geo.census.gov/geocoder/locations/address'

    def __init__(self, benchmark='Public_AR_Current', timeout=15):
        self.benchmark = benchmark
        self.timeout = timeout

    def geocode(self, address):
        provider = 'US Census Geocoder'
        if (not address.block_number.isdigit() or int(address.block_number) <= 0
                or not address.primary_street or address.intersecting_street):
            return GeocodeResult('unsupported', provider, details={
                'reason': 'Requires a positive block number and primary street, with no intersection',
                'input': asdict(address)})
        params = dict(street=f'{address.block_number} {census_street(address.primary_street)}',
                      city='San Diego', state='CA', benchmark=self.benchmark, format='json')
        request = Request(self.endpoint + '?' + urlencode(params),
                          headers={'User-Agent': 'CityLens/0.1 (public-data research)'})
        try:
            with urlopen(request, timeout=self.timeout) as response:
                payload = json.load(response)
            return self.parse_response(address, payload)
        except Exception as exc:
            # Error details are retained; no retry loop or fallback invents a point.
            return GeocodeResult('error', provider, details={
                'error': str(exc), 'benchmark': self.benchmark, 'request': params})

    def parse_response(self, address, payload):
        """Pure response validation, independently testable without live HTTP."""
        provider = 'US Census Geocoder'
        evidence = {'benchmark_requested': self.benchmark, 'response': payload,
                    'input': asdict(address),
                    'limitation': 'Interpolated block address; not an exact collision location'}
        matches = payload['result']['addressMatches']
        if not isinstance(matches, list):
            raise ValueError('Malformed addressMatches')
        if len(matches) != 1:
            return GeocodeResult('no_match' if not matches else 'ambiguous', provider, details=evidence)
        match = matches[0]
        components = match.get('addressComponents', {})
        street = ' '.join(filter(None, [components.get(key) for key in
            ('preQualifier','preDirection','preType','streetName','suffixType',
             'suffixDirection','suffixQualifier')]))
        # Census matchedAddress begins with the matched supplied structure number.
        number = match.get('matchedAddress', '').split(' ', 1)[0]
        lng, lat = float(match['coordinates']['x']), float(match['coordinates']['y'])
        if not (math.isfinite(lng) and math.isfinite(lat) and -180 <= lng <= 180 and -90 <= lat <= 90):
            raise ValueError('Invalid provider coordinates')
        reason=match_reason(number,street,components.get('city'),components.get('state'),
                            lng,lat,address.block_number,address.primary_street)
        if reason:
            evidence.update(reason=reason,validation_rejected=True)
            return GeocodeResult('ambiguous',provider,details=evidence)
        return GeocodeResult('matched',provider,lng,lat,'block_approximate',evidence)


def save_geocode(conn, report_id, address_key, result, *, only_if_pending=False):
    """Optimistically update only if the address still matches the queried input.

    Prevents a slow HTTP response from attaching coordinates to an address
    changed by a concurrent import. The queue worker also requires pending
    status so a slower concurrent worker cannot overwrite a completed result.
    Default behavior is retained for explicit callers refreshing a result.
    """
    if result.status not in {'matched','no_match','ambiguous','unsupported','error'}:
        raise ValueError('Invalid geocode status')
    if result.status == 'matched':
        if (result.longitude is None or result.latitude is None
                or not math.isfinite(result.longitude) or not math.isfinite(result.latitude)
                or not -180 <= result.longitude <= 180 or not -90 <= result.latitude <= 90
                or result.precision != 'block_approximate'):
            raise ValueError('Matched result needs valid approximate coordinates')
    elif result.longitude is not None or result.latitude is not None:
        raise ValueError('Unmatched results must not contain coordinates')
    elif result.precision is not None:
        raise ValueError('Unmatched results must not claim coordinate precision')
    cursor = conn.execute('''UPDATE citylens.traffic_collisions SET
        location=CASE WHEN %s='matched' THEN
            ST_SetSRID(ST_MakePoint(%s,%s),4326)::geography END,
        geocode_status=%s,geocode_provider=%s,geocode_precision=%s,
        geocoded_at=now(),geocode_details=%s
        WHERE report_id=%s AND address_key=%s
          AND (NOT %s OR geocode_status='pending')''',
        (result.status,result.longitude,result.latitude,result.status,result.provider,
         result.precision,Jsonb(result.details or {}),report_id,address_key,only_if_pending))
    return cursor.rowcount == 1


def geocode_pending(conn, provider: Provider, limit=25, pause=1.0, *, report_ids=None):
    """Process a bounded pending queue, caching repeated addresses within the run.

    Intended for an offline worker using an autocommit connection; HTTP calls
    should not hold a long-running database transaction. Failed/unmatched rows
    are not retried automatically. Reset their status explicitly to retry.
    """
    if not 1 <= limit <= 1000 or not math.isfinite(pause) or pause < 0:
        raise ValueError('limit must be 1..1000 and pause must be nonnegative')
    if report_ids is not None:
        if (not report_ids or len(report_ids)>limit or len(set(report_ids))!=len(report_ids)
                or any(not isinstance(value,str) or not value.strip() for value in report_ids)):
            raise ValueError('Selected report IDs must be nonempty, unique, and fit the limit')
        rows=conn.execute('''SELECT report_id,address_key,raw_record
            FROM citylens.traffic_collisions WHERE geocode_status='pending'
            AND report_id=ANY(%s) ORDER BY report_id LIMIT %s''',(report_ids,limit)).fetchall()
    else:
        rows = conn.execute('''SELECT report_id,address_key,raw_record
            FROM citylens.traffic_collisions WHERE geocode_status='pending'
            ORDER BY report_id LIMIT %s''',(limit,)).fetchall()
    cache, stats = {}, {}
    for report_id,key,record in rows:
        if key not in cache:
            cache[key] = provider.geocode(normalize_collision_address(record))
            time.sleep(pause)
        result = cache[key]
        if save_geocode(conn,report_id,key,result,only_if_pending=True):
            stats[result.status] = stats.get(result.status,0)+1
        else:
            stats['source_or_status_changed'] = stats.get('source_or_status_changed',0)+1
    return stats


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--limit',type=int,default=25)
    parser.add_argument('--benchmark',default='Public_AR_Current')
    parser.add_argument('--report-id',action='append',help='Optional explicit pending IDs; repeat per record')
    args = parser.parse_args()
    if __package__:
        from .config import load_environment
    else:
        from config import load_environment
    load_environment()
    with psycopg.connect(os.environ['DATABASE_URL'],connect_timeout=10,autocommit=True) as conn:
        print(json.dumps(geocode_pending(conn,CensusGeocoder(args.benchmark),args.limit,report_ids=args.report_id)))


if __name__ == '__main__':
    main()
