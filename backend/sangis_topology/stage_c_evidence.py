"""Bounded, resumable TIGERweb transportation evidence for Stage C.

One identify request per distinct proposed node position (the geographic query
scope), using exactly the Stage A/B request. Every response is cached with its
URL, raw body and SHA-256. Responses captured earlier are reused only when the
URL is identical and the original ledger checksum still matches the file.

Missing evidence and an exhausted budget stay `unknown`; transport/provider
failures stay `provider_error`. Neither is ever reported as a geographic
mismatch. No Google service and no Census address geocoder is used.
"""
import gzip
import hashlib
import json
import math
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, build_opener

from backend.agent.geocoding_http import NoRedirect
from backend.census_addresses import DIRECTIONS
from backend.scripts.audit_historical_locations import point_segment_distance
from backend.verification.intersection_feasibility.study import TIGER
from backend.verification.intersection_feasibility.topology import canonical, projected, segment_crossing
from .acquire import EvidenceError, now, read, save, sha

HALF_WIDTH_DEGREES = .002
PROXIMITY_M = 30          # Inherited Stage A/B diagnostic; see REPORT for limits.
MAX_RESULTS = 1000        # ArcGIS default page size; at/over it is treated as incomplete.
MAX_BYTES = 8_000_000
MAX_ATTEMPTS_PER_SCOPE = 2
TRANSIENT_HTTP = {429, 500, 502, 503, 504}
USER_AGENT = 'CityLens/StageC-independent-road-validation'


def scope_url(longitude, latitude):
    """The exact Stage A/B identify URL, so archived evidence stays reusable."""
    x, y, h = longitude, latitude, HALF_WIDTH_DEGREES
    bbox = dict(xmin=x - h, ymin=y - h, xmax=x + h, ymax=y + h, spatialReference=dict(wkid=4326))
    params = dict(geometry=json.dumps(bbox), geometryType='esriGeometryEnvelope', sr=4326,
                  layers='all:2,6,8', tolerance=0, mapExtent=f'{x - h},{y - h},{x + h},{y + h}',
                  imageDisplay='800,800,96', returnGeometry='true', returnFieldName='true', f='json')
    return TIGER + '/identify?' + urlencode(params)


def http_fetch(url, timeout):
    with build_opener(NoRedirect()).open(Request(url, headers={'User-Agent': USER_AGENT}), timeout=timeout) as response:
        raw = response.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError('oversized_response')
    return raw


def durable_save(path, value, *, attempts=20, sleep=time.sleep):
    """Atomic replace with a bounded retry for transient Windows file locks
    (indexers/antivirus). Never falls back to a non-atomic write."""
    for attempt in range(attempts):
        try:
            return save(path, value)
        except PermissionError:
            if attempt == attempts - 1:
                raise
            sleep(.25)


def _gzip_json(path, value):
    temporary = path.with_suffix(path.suffix + '.tmp')
    with gzip.open(temporary, 'wt', encoding='utf-8') as stream:
        json.dump(value, stream, separators=(',', ':'), allow_nan=False)
    temporary.replace(path)


class LedgerSource:
    """Previously captured responses, reusable only by exact URL + ledger SHA-256."""

    def __init__(self, name, directory, ledger_name):
        self.name, self.directory = name, Path(directory)
        self.by_url, self.rejected = {}, []
        path = self.directory / ledger_name
        if not path.exists():
            return
        for entry in read(path).get('attempts', []):
            if entry.get('status') == 'ok' and entry.get('file') and entry.get('sha256') and entry.get('url'):
                self.by_url[entry['url']] = entry

    def lookup(self, url):
        entry = self.by_url.get(url)
        if not entry:
            return None
        path = self.directory / str(entry['file']).replace('\\', '/')
        if not path.exists() or sha(path) != entry['sha256']:
            self.rejected.append({'url': url, 'reason': 'missing_or_checksum_mismatch'})
            return None
        with gzip.open(path, 'rt', encoding='utf-8') as stream:
            record = json.load(stream)
        if record.get('url') != url or record.get('status', 'ok') != 'ok' or 'response' not in record:
            self.rejected.append({'url': url, 'reason': 'record_identity_or_status_mismatch'})
            return None
        if 'raw_body_utf8' in record and json.loads(record['raw_body_utf8']) != record['response']:
            self.rejected.append({'url': url, 'reason': 'raw_body_differs_from_parsed_response'})
            return None
        return record, {'source': self.name, 'file': path.as_posix(), 'sha256': entry['sha256'],
                        'captured_at': record.get('captured_at')}


class EvidenceStore:
    def __init__(self, directory, *, budget, live=False, reuse=(), fetch=None, interval=.5, timeout=20, sleep=time.sleep):
        self.directory = Path(directory)
        (self.directory / 'responses').mkdir(parents=True, exist_ok=True)
        self.path = self.directory / 'requests.json'
        if self.path.exists():
            self.ledger = read(self.path)
            if self.ledger['budget'] != budget:
                raise EvidenceError('Persisted Stage C request budget differs; do not reset the ledger to bypass it')
        else:
            self.ledger = {'budget': budget, 'interval_seconds': interval, 'timeout_seconds': timeout,
                           'max_attempts_per_scope': MAX_ATTEMPTS_PER_SCOPE,
                           'retry_policy': 'One retry only for transport errors and HTTP 429/500/502/503/504. '
                                           'No retry for malformed, failed or incomplete responses or missing names.',
                           'entries': []}
        self.live, self.reuse = live, list(reuse)
        self.fetch, self.interval, self.timeout, self.sleep = fetch or http_fetch, interval, timeout, sleep
        self._last = 0.0

    @property
    def live_attempts(self):
        return sum(e['origin'] == 'live' for e in self.ledger['entries'])

    def _ok(self, url):
        found = [e for e in self.ledger['entries'] if e['url'] == url and e['status'] == 'ok']
        return found[-1] if found else None

    def _load(self, entry):
        path = self.directory / entry['file']
        if not path.exists() or sha(path) != entry['sha256']:
            raise EvidenceError('Stage C cached response corrupted or missing: ' + entry['file'])
        with gzip.open(path, 'rt', encoding='utf-8') as stream:
            record = json.load(stream)
        if record['url'] != entry['url']:
            raise EvidenceError('Stage C cached response URL mismatch')
        return {'status': 'ok', 'reason': None, 'payload': record['response'],
                'reference': {'file': entry['file'], 'sha256': entry['sha256'], 'origin': entry['origin'],
                              'captured_at': record.get('captured_at')}}

    def flush(self):
        durable_save(self.path, self.ledger)
        self._pending = 0

    def _store(self, url, record, entry, *, defer=False):
        target = self.directory / 'responses' / (sha_text(url) + '.json.gz')
        _gzip_json(target, record)
        entry.update(status='ok', file=target.relative_to(self.directory).as_posix(), sha256=sha(target), finished_at=now())
        # Reuse imports are re-derivable, so their ledger writes are batched.
        # Live captures are always journaled immediately.
        self._pending = getattr(self, '_pending', 0) + 1
        if not defer or self._pending >= 200:
            self.flush()
        return self._load(entry)

    def peek(self, url):
        """Planning only: where would evidence come from, without capturing it."""
        if self._ok(url):
            return 'cached'
        for source in self.reuse:
            if url in source.by_url:
                return source.name
        failures = [e for e in self.ledger['entries'] if e['url'] == url and e['origin'] == 'live']
        return 'previous_failure' if failures else 'new_request'

    def get(self, url):
        entry = self._ok(url)
        if entry:
            return self._load(entry)
        for source in self.reuse:
            hit = source.lookup(url)
            if hit:
                record, provenance = hit
                copy = {'url': url, 'captured_at': record.get('captured_at'), 'response': record['response'],
                        'reused_from': provenance}
                if 'raw_body_utf8' in record:
                    copy['raw_body_utf8'] = record['raw_body_utf8']
                entry = {'url': url, 'origin': 'reused:' + source.name, 'status': 'started', 'started_at': now(),
                         'source_file': provenance['file'], 'source_sha256': provenance['sha256']}
                self.ledger['entries'].append(entry)
                return self._store(url, copy, entry, defer=True)
        previous = [e for e in self.ledger['entries'] if e['url'] == url and e['origin'] == 'live']
        final = [e for e in previous if e['status'] == 'provider_error']
        if final:
            return {'status': 'provider_error', 'reason': final[-1]['reason'], 'payload': None, 'reference': None}
        if len(previous) >= MAX_ATTEMPTS_PER_SCOPE:
            return {'status': 'provider_error', 'reason': 'retry_limit_reached:' + previous[-1].get('reason', ''), 'payload': None, 'reference': None}
        if not self.live:
            return {'status': 'unknown', 'reason': 'offline_evidence_missing', 'payload': None, 'reference': None}
        while len(previous) < MAX_ATTEMPTS_PER_SCOPE:
            if self.live_attempts >= self.ledger['budget']:
                return {'status': 'unknown', 'reason': 'request_budget_exhausted', 'payload': None, 'reference': None}
            wait = self.interval - (time.monotonic() - self._last)
            if wait > 0:
                self.sleep(wait)
            entry = {'url': url, 'origin': 'live', 'status': 'started', 'attempt': len(previous) + 1, 'started_at': now()}
            self.ledger['entries'].append(entry)
            previous.append(entry)
            durable_save(self.path, self.ledger)  # Journal before network access.
            self._last = time.monotonic()
            try:
                raw = self.fetch(url, self.timeout)
            except HTTPError as exc:
                transient = exc.code in TRANSIENT_HTTP
                entry.update(status='transient_error' if transient else 'provider_error', reason=f'http_{exc.code}', finished_at=now())
                durable_save(self.path, self.ledger)
                if transient:
                    continue
                return {'status': 'provider_error', 'reason': entry['reason'], 'payload': None, 'reference': None}
            except (URLError, TimeoutError, OSError) as exc:
                entry.update(status='transient_error', reason='transport_' + type(exc).__name__, finished_at=now())
                durable_save(self.path, self.ledger)
                continue
            except ValueError as exc:
                entry.update(status='provider_error', reason=str(exc) or 'invalid_response', finished_at=now())
                durable_save(self.path, self.ledger)
                return {'status': 'provider_error', 'reason': entry['reason'], 'payload': None, 'reference': None}
            try:
                body = raw.decode('utf-8')
                payload = json.loads(body)
            except (UnicodeDecodeError, ValueError):
                entry.update(status='provider_error', reason='malformed_json', finished_at=now())
                durable_save(self.path, self.ledger)
                return {'status': 'provider_error', 'reason': 'malformed_json', 'payload': None, 'reference': None}
            error = payload.get('error') if isinstance(payload, dict) else None
            if isinstance(error, dict) and error.get('code') in TRANSIENT_HTTP:
                entry.update(status='transient_error', reason=f"provider_error_{error.get('code')}", finished_at=now())
                durable_save(self.path, self.ledger)
                continue
            # Complete responses (including provider error payloads) are cached so
            # the assessment of an incomplete/failed answer is itself reproducible.
            record = {'url': url, 'captured_at': now(), 'response': payload, 'raw_body_utf8': body}
            return self._store(url, record, entry)
        return {'status': 'provider_error', 'reason': 'retry_limit_reached:' + previous[-1].get('reason', ''), 'payload': None, 'reference': None}


def sha_text(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def parse_identify(payload):
    """Validate one complete identify answer. Returns (status, reason, features)."""
    if not isinstance(payload, dict) or payload.get('error') or not isinstance(payload.get('results'), list):
        return 'provider_error', 'malformed_or_error_response', []
    results = payload['results']
    if payload.get('exceededTransferLimit') or len(results) >= MAX_RESULTS:
        return 'unknown', 'possibly_incomplete_response', []
    features = []
    for feature in results:
        if not isinstance(feature, dict):
            return 'provider_error', 'malformed_feature', []
        if feature.get('geometryType') != 'esriGeometryPolyline':
            continue
        geometry = feature.get('geometry') if isinstance(feature.get('geometry'), dict) else {}
        sr = geometry.get('spatialReference') or {}
        if sr.get('latestWkid', sr.get('wkid')) != 4326:
            return 'unknown', 'unconfirmed_response_crs', []
        attributes = feature.get('attributes') if isinstance(feature.get('attributes'), dict) else {}
        name = attributes.get('NAME')
        name = name if isinstance(name, str) and name.strip() and name.strip().upper() != 'NULL' else None
        paths = []
        for path in geometry.get('paths') or []:
            points = [projected(p) for p in path] if isinstance(path, list) else []
            points = [p for p in points if p is not None]
            if len(points) >= 2:
                paths.append(points)
        features.append({'name': canonical(name) if name else None, 'raw_name': name, 'mtfcc': attributes.get('MTFCC'),
                         'layer': feature.get('layerId'), 'paths': paths})
    return 'ok', None, features


def _segments(features):
    return [(a, b) for f in features for path in f['paths'] for a, b in zip(path, path[1:])]


def _distance(point, features):
    values = [point_segment_distance(point, a, b) for a, b in _segments(features)]
    return min(values) if values else None


def _segment_distance(a, b, c, d):
    if segment_crossing(a, b, c, d) is not None:
        return 0.0
    return min(point_segment_distance(a, c, d), point_segment_distance(b, c, d),
               point_segment_distance(c, a, b), point_segment_distance(d, a, b))


def _bearing(point, features):
    best = None
    for a, b in _segments(features):
        d = point_segment_distance(point, a, b)
        if best is None or d < best[0]:
            best = (d, math.degrees(math.atan2(b[1] - a[1], b[0] - a[0])) % 180)
    return best[1] if best else None


def _directional_conflict(requested, observed):
    req, obs = requested.split(), observed.split()
    req_dirs, obs_dirs = {t for t in req if t in DIRECTIONS}, {t for t in obs if t in DIRECTIONS}
    base = lambda tokens: [t for t in tokens if t not in DIRECTIONS]
    # Exclusion evidence only: never used to accept a different spelling.
    return base(req) == base(obs) and req_dirs and obs_dirs and req_dirs != obs_dirs


def assess(primary, cross, longitude, latitude, evidence):
    """Independent named-road check for one proposed junction."""
    if evidence['status'] != 'ok':
        return {'status': evidence['status'], 'reason': evidence['reason'], 'road_checks': {}, 'diagnostics': {}}
    status, reason, features = parse_identify(evidence['payload'])
    if status != 'ok':
        return {'status': status, 'reason': reason, 'road_checks': {}, 'diagnostics': {}}
    point = projected([longitude, latitude])
    roads, near = {}, {}
    for street in (primary, cross):
        lines = [f for f in features if f['name'] == canonical(street)]
        distance = _distance(point, lines)
        within = [f for f in lines if (_distance(point, [f]) or math.inf) <= PROXIMITY_M]
        near[street] = within
        roads[street] = {'matching_features': len(lines), 'distance_m': round(distance, 3) if distance is not None else None,
                         'within_30m_features': len(within), 'mtfcc': sorted({str(f['mtfcc']) for f in within})}
    named_nearby = sorted({f['name'] for f in features if f['name'] and (_distance(point, [f]) or math.inf) <= PROXIMITY_M})
    diagnostics = {'response_polyline_features': len(features), 'named_roads_within_30m': named_nearby}
    if all(near.values()):
        meet = min(_segment_distance(a, b, c, d) for a, b in _segments(near[primary]) for c, d in _segments(near[cross]))
        first, second = _bearing(point, near[primary]), _bearing(point, near[cross])
        angle = abs(first - second) % 180
        diagnostics.update(named_lines_min_separation_m=round(meet, 3), crossing_angle_degrees=round(min(angle, 180 - angle), 1))
        return {'status': 'pass', 'reason': 'both_named_roads_within_30m', 'road_checks': roads, 'diagnostics': diagnostics}
    if any(r['matching_features'] and r['distance_m'] is not None and r['distance_m'] > PROXIMITY_M for r in roads.values()):
        return {'status': 'fail', 'reason': 'named_road_geometry_inconsistent_with_junction', 'road_checks': roads, 'diagnostics': diagnostics}
    for street in (primary, cross):
        if not near[street] and any(_directional_conflict(canonical(street), name) for name in named_nearby):
            return {'status': 'fail', 'reason': 'conflicting_directional_name_at_junction', 'road_checks': roads, 'diagnostics': diagnostics}
    if any(r['matching_features'] and r['distance_m'] is None for r in roads.values()):
        return {'status': 'unknown', 'reason': 'named_road_geometry_unusable', 'road_checks': roads, 'diagnostics': diagnostics}
    return {'status': 'unknown', 'reason': 'road_name_not_identified_in_independent_source', 'road_checks': roads, 'diagnostics': diagnostics}
