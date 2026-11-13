"""Reproducible, local CSV -> SQLite projection for the course explorer.

The source CSV and existing PostGIS database are never modified. Last CSV row
per request ID wins, including when that row moves outside the selected scope.
Dates are source-local Los Angeles wall dates. No network/API keys are needed.
"""
import csv
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / 'backend/Raw Data/Get it done/get_it_done_requests_CD1_datasd.csv'
CACHE = ROOT / '.local/explorer.sqlite3'
VERSION = 1
REQUIRED = {'service_request_id', 'date_requested', 'council_district', 'lat', 'lng',
            'service_name', 'status', 'zipcode', 'comm_plan_name', 'date_closed',
            'service_request_parent_id', 'street_address'}


def text(value):
    return ' '.join((value or '').split()) or None


def source_date(value):
    try:
        return datetime.fromisoformat(value.strip()).isoformat(sep=' ', timespec='seconds')
    except (ValueError, AttributeError):
        return None


def normalize(row):
    """Return a scoped public record, preserving unknown fields as NULL."""
    request_id = text(row.get('service_request_id'))
    requested = source_date(row.get('date_requested'))
    if not request_id or not requested or row.get('council_district', '').strip() != '1' or requested[:4] != '2026':
        return None
    try:
        lat, lng = float(row['lat']), float(row['lng'])
        if not (math.isfinite(lat) and math.isfinite(lng) and -90 <= lat <= 90 and -180 <= lng <= 180):
            raise ValueError('Invalid coordinates')
    except (ValueError, TypeError, KeyError):
        lat = lng = None
    return (request_id, requested, text(row.get('service_name')), text(row.get('status')),
            text(row.get('zipcode')), text(row.get('comm_plan_name')), lat, lng,
            text(row.get('street_address')), source_date(row.get('date_closed')),
            text(row.get('service_request_parent_id')))


def signature(source):
    s = source.stat()
    return {'size': s.st_size, 'mtime_ns': s.st_mtime_ns, 'source': str(source.resolve()), 'version': VERSION}


def build_cache(source=SOURCE, target=CACHE):
    """Atomically rebuild a disposable cache; retain an old cache on failure."""
    source, target = Path(source), Path(target)
    before = signature(source)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix='explorer-', suffix='.sqlite3', dir=target.parent)
    os.close(fd)
    stats = {'source_rows': 0, 'invalid_dates': 0, 'missing_ids': 0}
    try:
        with sqlite3.connect(temp) as db:
            db.executescript('''
                CREATE TABLE requests (
                    id TEXT PRIMARY KEY, requested TEXT NOT NULL, category TEXT, status TEXT,
                    zipcode TEXT, community TEXT, lat REAL, lng REAL, address TEXT,
                    closed TEXT, parent_id TEXT);
                CREATE TABLE metadata (value TEXT NOT NULL);
            ''')
            with source.open(encoding='utf-8-sig', newline='') as f:
                reader = csv.DictReader(f)
                if not REQUIRED.issubset(reader.fieldnames or []):
                    raise ValueError('Missing required CSV columns: ' + ', '.join(sorted(REQUIRED - set(reader.fieldnames or []))))
                for row in reader:
                    if None in row or any(v is None for v in row.values()):
                        raise ValueError('Malformed CSV row; cache was not replaced')
                    stats['source_rows'] += 1
                    stats['missing_ids'] += not bool(text(row['service_request_id']))
                    stats['invalid_dates'] += source_date(row['date_requested']) is None
                    record = normalize(row)
                    if record:
                        db.execute('INSERT OR REPLACE INTO requests VALUES (?,?,?,?,?,?,?,?,?,?,?)', record)
                    else:
                        db.execute('DELETE FROM requests WHERE id=?', (text(row['service_request_id']),))
            db.executescript('''
                CREATE INDEX requests_date ON requests(requested, id);
                CREATE INDEX requests_category ON requests(category, requested);
                CREATE INDEX requests_status ON requests(status, requested);
                CREATE INDEX requests_community ON requests(community, requested);
                CREATE INDEX requests_zip ON requests(zipcode, requested);
                CREATE INDEX requests_location ON requests(lat, lng);
            ''')
            with source.open('rb') as f:
                digest = hashlib.file_digest(f, 'sha256').hexdigest()
            if before != signature(source):
                raise ValueError('Source changed during processing; retry')
            stats.update(dict(zip(['records', 'first_date', 'last_date', 'unlocated', 'linked_requests'],
                db.execute('SELECT count(*),min(requested),max(requested),sum(lat IS NULL),sum(parent_id IS NOT NULL) FROM requests').fetchone())))
            stats.update(signature=before, sha256=digest, built_at=datetime.now(timezone.utc).isoformat(),
                         source_name=source.name, year=2026, district=1)
            db.execute('INSERT INTO metadata VALUES (?)', (json.dumps(stats),))
            db.execute('ANALYZE')
        os.replace(temp, target)
        return stats
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


@contextmanager
def connect(path=None):
    db = sqlite3.connect(Path(path or CACHE).resolve().as_uri() + '?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    try:
        yield db
    finally:
        db.close()


def ensure_cache(source=SOURCE, target=CACHE):
    try:
        with connect(target) as db:
            meta = json.loads(db.execute('SELECT value FROM metadata').fetchone()[0])
        if meta['signature'] == signature(Path(source)):
            return meta
    except (sqlite3.Error, KeyError, ValueError, OSError):
        pass
    return build_cache(source, target)


if __name__ == '__main__':
    print(json.dumps(build_cache(), indent=2))
