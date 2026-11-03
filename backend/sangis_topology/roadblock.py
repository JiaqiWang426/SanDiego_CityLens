"""Deterministic SanGIS road-block candidates for unlocated numbered reports.

Read-only. The collision source number is an anonymized BLOCK number (the City
dictionary abstracts primary numbers to block level), so a report N names the
block N..N+99, never the building at house number N. A candidate is accepted
only when complete, exact (or officially abbreviated, same-record) road names,
address ranges, City of San Diego jurisdiction, constructed/maintained status,
connectivity and a compact extent leave exactly one block location.

The representative point lies ON the supporting road geometry, closest to the
block's centroid. It is a block-approximate position, not a crash location.
Distant candidates are never averaged and parcel/address points are never used.
"""
from collections import Counter, defaultdict
import re

from backend.verification.intersection_feasibility.topology import canonical

METHOD_VERSION = 'sangis-roadblock-v1'
MAX_BLOCK_EXTENT_M = 400       # Longest accepted span of the supporting block geometry.
CARRIAGEWAY_GAP_M = 60         # Components closer than this form one block (divided roads, short gaps).
US_FOOT_M = 1200 / 3937
SUPPORTED_STATUS = {'C', 'M'}
SUPPORTED_CLASS = {'2', '3', '4', '5', '6', '7', 'A', 'M', 'Z'}   # Excludes freeway 1, ramps 8/9, paper/undocumented codes.
FREEWAY_CLASS = {'1', '8', '9'}
CITY = 'SD'

# Offsets/compound descriptions. Real names such as QUARTER MILE DR, I ST or
# PACIFIC HWY are NOT matched; freeway segments are rejected by SanGIS class.
OFFSET = re.compile(r'\b(?:FT|FEET|OF|NCL|SCL|ECL|WCL|BLOCK|PARKING LOT)\b|[&/()]')
FREEWAY = re.compile(r'\b(?:RAMP|RMP|ONRAMP|OFFRAMP|INTERSTATE|FWY|FREEWAY|STATE ROUTE)\b|\bI-\s?\d|\bI\s\d|\bSR-?\s?\d')


def parse_source(primary_street, number, intersecting_street):
    """Return (reason or None, canonical street, block)."""
    text = (primary_street or '').strip().upper()
    raw = (number or '').strip()
    if (intersecting_street or '').strip():
        return 'cross_street_present_not_a_block_address', None, None
    if not text:
        return 'missing_primary_street', None, None
    if OFFSET.search(text):
        return 'descriptive_offset_or_compound_description', None, None
    if FREEWAY.search(text):
        return 'freeway_or_ramp_description', None, None
    if not raw.isdigit() or int(raw) <= 0:
        return 'no_positive_block_number', None, None
    if int(raw) % 100:
        # The dictionary abstracts numbers to blocks; anything else may be an
        # offset in feet or an unabstracted value and is not reinterpreted.
        return 'number_not_block_abstracted', None, None
    return None, canonical(text), int(raw)


def side_ranges(seg):
    out = []
    for low, high in ((seg['llowaddr'], seg['lhighaddr']), (seg['rlowaddr'], seg['rhighaddr'])):
        if isinstance(low, int) and isinstance(high, int) and low > 0 and high > 0:
            out.append((min(low, high), max(low, high)))
    return out


def block_compatible(seg, block):
    return any(low <= block + 99 and high >= block for low, high in side_ranges(seg))


class NameIndex:
    """Exact canonical names. rd30full must agree with its own components;
    rd20full is SanGIS' official abbreviation recorded on the SAME segment."""

    def __init__(self, segments):
        self.by_name = defaultdict(list)
        for seg in segments:
            full = seg['rd30full']
            parts = ' '.join(p for p in (seg['rd30pred'], seg['rd30name'], seg['rd30sfx'], seg['rd30postd']) if p)
            if full and seg['rd30name'] and canonical(full) == canonical(parts):
                self.by_name[canonical(full)].append((seg, 'exact_rd30full'))
            if seg['rd20full'] and full and canonical(seg['rd20full']) != canonical(full):
                self.by_name[canonical(seg['rd20full'])].append((seg, 'official_rd20full_abbreviation'))

    def lookup(self, name):
        return self.by_name.get(name, [])


def components(ids, segments, distances):
    """Union-find over node connectivity (exact shared endpoint IDs) and, as a
    second relation, geometric proximity under CARRIAGEWAY_GAP_M."""
    parent = {i: i for i in ids}

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(a, b):
        parent[find(a)] = find(b)
    nodes = defaultdict(list)
    for i in ids:
        for node in (segments[i]['fnode'], segments[i]['tnode']):
            if node and node > 0:
                nodes[node].append(i)
    for group in nodes.values():
        for other in group[1:]:
            union(group[0], other)
    connected = len({find(i) for i in ids})
    for (a, b), d in distances.items():
        if d <= CARRIAGEWAY_GAP_M:
            union(a, b)
    clusters = defaultdict(list)
    for i in ids:
        clusters[find(i)].append(i)
    return connected, sorted(sorted(c) for c in clusters.values())


GEOMETRY_SQL = '''WITH s AS (SELECT roadsegid, ST_Force2D(geom) AS g FROM citylens_ref.road_segments WHERE snapshot_id=%s AND roadsegid=ANY(%s))
SELECT a.roadsegid AS a, b.roadsegid AS b, ST_Distance(a.g,b.g) AS ft FROM s a JOIN s b ON a.roadsegid<b.roadsegid'''

BLOCK_SQL = '''WITH s AS (SELECT ST_Force2D(geom) AS g, ST_IsValid(geom) AS v, ST_IsSimple(geom) AS simple FROM citylens_ref.road_segments
                WHERE snapshot_id=%s AND roadsegid=ANY(%s)),
     u AS (SELECT ST_Collect(g) AS g, bool_and(v) AS valid, bool_and(simple) AS simple, count(*) AS n FROM s),
     p AS (SELECT ST_ClosestPoint(g, ST_Centroid(g)) AS pt, g, valid, simple, n FROM u)
SELECT valid, simple, n, ST_MaxDistance(g,g) AS extent_ft, ST_MaxDistance(pt,g) AS radius_ft, ST_Length(g) AS length_ft,
       ST_X(ST_Transform(ST_SetSRID(pt,2230),4326)) AS longitude, ST_Y(ST_Transform(ST_SetSRID(pt,2230),4326)) AS latitude FROM p'''


def resolve_block(conn, sid, index, name, block):
    """Classify one (canonical street, block) query. Shared by all reports."""
    matches = index.lookup(name)
    result = {'street': name, 'block': block, 'status': None, 'reason': None, 'segment_ids': [], 'name_sources': [],
              'diagnostics': {}}
    if not matches:
        return dict(result, status='street_not_found', reason='no_exact_or_official_abbreviated_name')
    city = [(s, how) for s, how in matches if CITY in (s['ljurisdic'], s['rjurisdic'])]
    compatible_anywhere = [(s, how) for s, how in matches if block_compatible(s, block)]
    result['diagnostics'].update(name_segments=len(matches), city_name_segments=len(city),
                                 outside_city_compatible=sum(CITY not in (s['ljurisdic'], s['rjurisdic']) for s, _ in compatible_anywhere),
                                 rd30_names=sorted({canonical(s['rd30full']) for s, _ in matches}))
    if not city:
        return dict(result, status='street_not_found', reason='name_not_in_city_of_san_diego')
    compatible = [(s, how) for s, how in city if block_compatible(s, block)]
    if not compatible:
        return dict(result, status='insufficient_evidence', reason='block_outside_city_address_ranges')
    if any(s['segclass'] in FREEWAY_CLASS for s, _ in compatible):
        return dict(result, status='invalid_source', reason='freeway_or_ramp_segment_in_block')
    unsupported = [s for s, _ in compatible if s['segstat'] not in SUPPORTED_STATUS or s['segclass'] not in SUPPORTED_CLASS]
    supported = [(s, how) for s, how in compatible if s['segstat'] in SUPPORTED_STATUS and s['segclass'] in SUPPORTED_CLASS]
    result['diagnostics']['unsupported_status_or_class_in_block'] = sorted(s['roadsegid'] for s in unsupported)
    if not supported:
        return dict(result, status='insufficient_evidence', reason='block_only_on_unconstructed_or_unsupported_segments')
    if len({canonical(s['rd30full']) for s, _ in supported}) > 1:
        return dict(result, status='source_conflict', reason='name_maps_to_several_official_roads')
    by_id = {s['roadsegid']: s for s, _ in supported}
    ids = sorted(by_id)
    result['segment_ids'] = ids
    result['name_sources'] = sorted({how for _, how in supported})
    distances = {(r['a'], r['b']): r['ft'] * US_FOOT_M for r in conn.execute(GEOMETRY_SQL, (sid, ids))} if len(ids) > 1 else {}
    connected, clusters = components(ids, by_id, distances)
    result['diagnostics'].update(node_connected_components=connected, clusters=len(clusters))
    if len(clusters) > 1:
        separation = min(d for (a, b), d in distances.items() if not any(a in c and b in c for c in clusters))
        result['diagnostics']['min_cluster_separation_m'] = round(separation, 1)
        return dict(result, status='multiple_candidates', reason='geographically_separated_compatible_segments')
    row = conn.execute(BLOCK_SQL, (sid, ids)).fetchone()
    if not row or not row['valid'] or not row['simple'] or row['longitude'] is None:
        return dict(result, status='provider_or_data_error', reason='invalid_or_nonsimple_block_geometry')
    extent = row['extent_ft'] * US_FOOT_M
    result['diagnostics'].update(extent_m=round(extent, 1), uncertainty_radius_m=round(row['radius_ft'] * US_FOOT_M, 1),
                                 road_length_m=round(row['length_ft'] * US_FOOT_M, 1),
                                 address_ranges=sorted({r for s in by_id.values() for r in side_ranges(s)}),
                                 police_beats=sorted({b for s in by_id.values() for b in (s['l_beat'], s['r_beat']) if b}),
                                 official_names=sorted({canonical(n) for s in by_id.values() for n in (s['rd30full'], s['rd20full']) if n}))
    if extent > MAX_BLOCK_EXTENT_M:
        return dict(result, status='range_ambiguous', reason='block_extent_exceeds_400m')
    return dict(result, status='accepted_candidate', reason='unique_compact_city_block', longitude=row['longitude'], latitude=row['latitude'])


SEGMENT_COLUMNS = '''SELECT roadsegid, rd30full, rd30pred, rd30name, rd30sfx, rd30postd, fnode, tnode, segstat, segclass,
  attributes->>'rd20full' AS rd20full, attributes->>'ljurisdic' AS ljurisdic, attributes->>'rjurisdic' AS rjurisdic,
  (attributes->>'llowaddr')::int AS llowaddr, (attributes->>'lhighaddr')::int AS lhighaddr,
  (attributes->>'rlowaddr')::int AS rlowaddr, (attributes->>'rhighaddr')::int AS rhighaddr,
  (attributes->>'l_beat')::int AS l_beat, (attributes->>'r_beat')::int AS r_beat
  FROM citylens_ref.road_segments WHERE snapshot_id=%s'''


def load_index(conn, sid):
    return NameIndex(conn.execute(SEGMENT_COLUMNS, (sid,)).fetchall())


# ---------------------------------------------------------------- Census evidence

def census_number_and_street(details):
    matched = (details or {}).get('matched_address')
    if not matched:
        return None, None
    first = matched.split(',')[0].split()
    if not first or not first[0].isdigit():
        return None, None
    return int(first[0]), canonical(' '.join(first[1:]))


def census_point(details):
    raw = (details or {}).get('raw_row') or []
    if len(raw) >= 6 and isinstance(raw[5], str) and ',' in raw[5]:
        try:
            x, y = (float(v) for v in raw[5].split(','))
            return x, y
        except ValueError:
            return None
    return None


def census_relation(status, details, source_street, block, official_names):
    """Map stored Census evidence to a relation. Never edits that evidence."""
    if status == 'no_match':
        return 'census_no_match', None
    reason = (details or {}).get('reason')
    if reason == 'provider_tie':
        return 'census_tie_without_candidates', None
    number, street = census_number_and_street(details)
    if number is None:
        return 'census_evidence_incomplete', 'insufficient_evidence'
    if number // 100 * 100 != block:
        return 'census_number_in_different_block', 'number_conflict'
    if street == source_street:
        return 'census_same_street_same_block', None
    if street in official_names:
        return 'census_documented_equivalent_street', None
    return 'census_matched_different_street', 'street_conflict'


AMBIGUOUS_OUTCOME = {'invalid_source': 'unsupported_description', 'street_not_found': 'insufficient_evidence',
                     'insufficient_evidence': 'insufficient_evidence', 'range_ambiguous': 'insufficient_evidence',
                     'multiple_candidates': 'multiple_candidates', 'source_conflict': 'insufficient_evidence',
                     'provider_or_data_error': 'insufficient_evidence'}


def summarize(results):
    return dict(Counter(r['status'] for r in results))
