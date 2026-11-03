"""Bounded, resumable ArcGIS extraction with a frozen ID inventory.

Native geometries and every source attribute are retained. A snapshot is usable
only after BOTH layers' counts, IDs, schemas and edit timestamps remain stable
through the whole acquisition. ArcGIS provides no cross-layer atomic snapshot.
"""
import gzip
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, build_opener

from backend.agent.geocoding_http import NoRedirect

LAYERS = {name: f'https://geo.sandag.org/server/rest/services/Hosted/{name}/FeatureServer/0'
          for name in ('Roads_All', 'Roads_Intersection')}
DEFAULT_DATA = Path(__file__).resolve().parents[1] / 'reference_data' / 'sangis'
MAX_REQUESTS = 240


def now():
    return datetime.now(timezone.utc).isoformat()


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False, default=str) + '\n', encoding='utf-8')
    temporary.replace(path)


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


class EvidenceError(RuntimeError):
    pass


class Capture:
    def __init__(self, directory, cap=MAX_REQUESTS, live=False):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / 'requests.json'
        self.ledger = read(self.path) if self.path.exists() else {'cap': cap, 'attempts': []}
        if self.ledger['cap'] != cap:
            raise EvidenceError('Persisted request cap differs; do not reset the ledger to bypass it')
        self.live = live

    def query(self, endpoint, params, kind, *, fresh=False):
        url = endpoint + '?' + urlencode(params)
        target = self.directory / 'cache' / (kind + '-' + digest(url)[:24] + '.json.gz')
        target.parent.mkdir(exist_ok=True)
        if target.exists() and not fresh:
            relative = target.relative_to(self.directory).as_posix()
            entries = [entry for entry in self.ledger['attempts'] if str(entry.get('file','')).replace('\\','/')==relative and entry.get('status')=='ok']
            if not entries or sha(target)!=entries[-1]['sha256']:
                raise EvidenceError('Cached response checksum differs from request ledger')
            with gzip.open(target, 'rt', encoding='utf-8') as stream:
                record = json.load(stream)
            if record['url'] != url:
                raise EvidenceError('Cache URL mismatch')
            payload = record['response']
        else:
            if not self.live:
                raise EvidenceError('Offline evidence missing: ' + kind)
            if len(self.ledger['attempts']) >= self.ledger['cap']:
                raise EvidenceError('Persisted REST request budget exhausted')
            entry = {'kind': kind, 'url': url, 'started_at': now(), 'status': 'started'}
            self.ledger['attempts'].append(entry)
            save(self.path, self.ledger)
            try:
                with build_opener(NoRedirect()).open(Request(url, headers={'User-Agent': 'CityLens/StageB-reference-audit'}), timeout=40) as response:
                    raw = response.read(40_000_001)
                if len(raw) > 40_000_000:
                    raise EvidenceError('Oversized response')
                payload = json.loads(raw)
                validate_response(payload)
                temporary = target.with_suffix('.tmp')
                with gzip.open(temporary, 'wt', encoding='utf-8') as stream:
                    json.dump({'url': url, 'captured_at': now(), 'response': payload}, stream, separators=(',', ':'), allow_nan=False)
                temporary.replace(target)
                entry.update(status='ok', file=str(target.relative_to(self.directory)), sha256=sha(target), finished_at=now())
            except Exception as exc:
                entry.update(status='failed', error_type=type(exc).__name__, finished_at=now())
                raise EvidenceError('Acquisition failed: ' + kind + ' (' + type(exc).__name__ + ')') from None
            finally:
                save(self.path, self.ledger)
        validate_response(payload)
        return payload


def validate_response(payload):
    if not isinstance(payload, dict) or payload.get('error') or payload.get('exceededTransferLimit'):
        raise EvidenceError('Malformed, failed or truncated REST response')


def identity(metadata):
    return {k: metadata.get(k) for k in ('fields', 'geometryType', 'extent', 'editingInfo', 'objectIdField', 'hasZ', 'hasM')}


def checked_ids(payload, count):
    ids = payload.get('objectIds')
    if (not isinstance(ids, list) or len(ids) != count or len(set(ids)) != count
            or any(type(i) is not int or i <= 0 for i in ids)):
        raise EvidenceError('Invalid or incomplete source ID inventory')
    return sorted(ids)


def validate_batch(payload, expected, metadata):
    validate_response(payload)
    sr = payload.get('spatialReference') or {}
    if sr.get('latestWkid', sr.get('wkid')) != 2230:
        raise EvidenceError('Native EPSG:2230 response required')
    features = payload.get('features')
    fields = {f['name'] for f in metadata['fields']}
    oid = metadata['objectIdField']
    if not isinstance(features, list):
        raise EvidenceError('Missing features')
    ids = []
    for feature in features:
        if not isinstance(feature, dict) or not isinstance(feature.get('attributes'), dict) or set(feature['attributes']) != fields:
            raise EvidenceError('Feature schema mismatch')
        value = feature['attributes'].get(oid)
        if type(value) is not int:
            raise EvidenceError('Noninteger object ID')
        ids.append(value)
    if len(ids) != len(set(ids)) or set(ids) != set(expected):
        raise EvidenceError('Missing, duplicate or unexpected batch IDs')
    return features


def acquire(directory=DEFAULT_DATA):
    directory = Path(directory)
    capture = Capture(directory, live=True)
    manifest_path = directory / 'manifest.json'
    if manifest_path.exists() and read(manifest_path).get('complete'):
        verify_files(directory)
        return read(manifest_path)
    plan_path = directory / 'acquisition-plan.json'
    if plan_path.exists():
        plan = read(plan_path)
        for name, info in plan['layers'].items():
            latest = capture.query(LAYERS[name], {'f': 'json'}, name + '-resume-metadata', fresh=True)
            if identity(latest) != identity(info['metadata']):
                raise EvidenceError('Provider version changed during acquisition; use a new snapshot directory')
    else:
        plan = {'started_at': now(), 'layers': {}}
        # Freeze both layers before downloading any batches.
        for name, url in LAYERS.items():
            metadata = capture.query(url, {'f': 'json'}, name + '-before-metadata')
            if metadata.get('hasM') or metadata.get('extent', {}).get('spatialReference', {}).get('latestWkid') != 2230:
                raise EvidenceError('Unexpected source dimensionality or CRS')
            count = capture.query(url + '/query', {'f': 'json', 'where': '1=1', 'returnCountOnly': 'true'}, name + '-before-count')['count']
            ids = checked_ids(capture.query(url + '/query', {'f': 'json', 'where': '1=1', 'returnIdsOnly': 'true'}, name + '-before-ids'), count)
            plan['layers'][name] = {'metadata': metadata, 'count': count, 'ids': ids}
        save(plan_path, plan)
    manifest = {'format_version': 1, 'complete': False, 'started_at': plan['started_at'], 'layers': {},
                'attribution': 'SanGIS and SANDAG; individual layer credits preserved in metadata.',
                'license': {'terms_url': 'https://gis.sangis.org/sanportal/apps/storymaps/stories/d26146d84e834ff6bcd58e4e620a983a',
                            'sangis_notice_url': 'https://www.sangis.org/Legal_Notice.htm',
                            'basis': 'Official public GIS web services, subject to provider use limitations and disclaimers; no accuracy or historical currency warranty. Item licenseInfo archived.'},
                'snapshot_limitation': 'No cross-layer transactional historicMoment is offered. Both ID inventories, counts, schemas and edit timestamps are checked before and after extraction. Different layer publishing timestamps are retained; this is a stable acquisition window, not proof of simultaneous source surveying.',
                'files': {}}
    for name, info in plan['layers'].items():
        metadata, ids = info['metadata'], info['ids']
        save(directory / (name + '-metadata.json'), metadata)
        item = capture.query('https://geo.sandag.org/portal/sharing/rest/content/items/' + metadata['serviceItemId'], {'f': 'json'}, name + '-item')
        save(directory / (name + '-item.json'), item)
        size = min(2000, metadata['maxRecordCount'])
        files = []
        for start in range(0, len(ids), size):
            group = ids[start:start + size]
            params = {'f': 'json', 'objectIds': ','.join(map(str, group)), 'outFields': '*',
                      'returnGeometry': 'true', 'returnZ': 'true', 'outSR': 2230}
            kind = name + '-batch-' + str(start // size)
            payload = capture.query(LAYERS[name] + '/query', params, kind)
            validate_batch(payload, group, metadata)
            files.append('cache/' + kind + '-' + digest(LAYERS[name] + '/query?' + urlencode(params))[:24] + '.json.gz')
            if start // size % 10 == 0:
                print(f'{name}: {min(start + size, len(ids))}/{len(ids)} features checked', flush=True)
        manifest['layers'][name] = {'source_url': LAYERS[name], 'feature_count': len(ids), 'id_sha256': digest(ids),
                                    'crs': 'EPSG:2230 (NAD83 / California zone 6, US survey feet)',
                                    'source_editing_info': metadata.get('editingInfo'), 'source_item_modified': item.get('modified'),
                                    'license_info': item.get('licenseInfo'), 'copyright': metadata.get('copyrightText'), 'batch_files': files}
    for name, info in plan['layers'].items():
        metadata = capture.query(LAYERS[name], {'f': 'json'}, name + '-after-metadata', fresh=True)
        count = capture.query(LAYERS[name] + '/query', {'f': 'json', 'where': '1=1', 'returnCountOnly': 'true'}, name + '-after-count', fresh=True)['count']
        ids = checked_ids(capture.query(LAYERS[name] + '/query', {'f': 'json', 'where': '1=1', 'returnIdsOnly': 'true'}, name + '-after-ids', fresh=True), count)
        if identity(metadata) != identity(info['metadata']) or count != info['count'] or ids != info['ids']:
            raise EvidenceError('Source changed within acquisition window; snapshot is incomplete')
    for path in sorted(directory.rglob('*.json*')):
        if path.name not in {'manifest.json', 'requests.json'} and not path.name.endswith('.tmp'):
            manifest['files'][path.relative_to(directory).as_posix()] = sha(path)
    manifest.update(complete=True, finished_at=now())
    manifest['snapshot_id'] = digest({'layers': manifest['layers'], 'files': manifest['files']})
    save(manifest_path, manifest)
    return manifest


def verify_files(directory):
    manifest = read(Path(directory) / 'manifest.json')
    if not manifest.get('complete'):
        raise EvidenceError('Acquisition not complete')
    if manifest['snapshot_id'] != digest({'layers': manifest['layers'], 'files': manifest['files']}):
        raise EvidenceError('Manifest identity mismatch')
    for name, checksum in manifest['files'].items():
        path = Path(directory) / name
        if path.resolve().is_relative_to(Path(directory).resolve()) is False or sha(path) != checksum:
            raise EvidenceError('Snapshot file missing or checksum mismatch')
    return manifest


def layer_features(directory, layer, manifest):
    metadata = read(Path(directory) / (layer + '-metadata.json'))
    seen = set()
    for name in manifest['layers'][layer]['batch_files']:
        with gzip.open(Path(directory) / name, 'rt', encoding='utf-8') as stream:
            features = json.load(stream)['response']['features']
        for feature in features:
            oid = feature['attributes'][metadata['objectIdField']]
            if oid in seen:
                raise EvidenceError('Repeated object ID across batches')
            seen.add(oid)
            yield feature
    if len(seen) != manifest['layers'][layer]['feature_count'] or digest(sorted(seen)) != manifest['layers'][layer]['id_sha256']:
        raise EvidenceError('Layer inventory mismatch')
