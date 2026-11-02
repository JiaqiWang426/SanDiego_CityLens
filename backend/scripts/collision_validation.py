"""Offline, conservative pilot diagnostics; never changes provider status or points.

SanGIS field names come from the archived Roads_All/FeatureServer/0 schema.
All input geometry is longitude/latitude (ArcGIS outSR=4326); distances use
WGS84 / UTM zone 11N, EPSG:32611, in metres. No snapping or interpolation.
"""
import math

from pyproj import Transformer

from backend.collision_addresses import normalize_street_name

TO_METRES = Transformer.from_crs(4326, 32611, always_xy=True)
REVIEW_DISTANCE_M = 50.0


def point_segment_distance(point, start, end):
    """Euclidean distance in the coordinates' units, including zero-length lines."""
    dx, dy = end[0]-start[0], end[1]-start[1]
    square = dx*dx+dy*dy
    fraction = max(0, min(1, ((point[0]-start[0])*dx+(point[1]-start[1])*dy)/square)) if square else 0
    return math.hypot(point[0]-start[0]-fraction*dx, point[1]-start[1]-fraction*dy)


def road_distance_m(longitude, latitude, paths):
    point = TO_METRES.transform(longitude, latitude)
    distances = []
    for path in paths:
        projected = [TO_METRES.transform(*vertex[:2]) for vertex in path]
        distances.extend(point_segment_distance(point, a, b) for a, b in zip(projected, projected[1:]))
    return min(distances, default=math.inf)


def numeric_range(low, high):
    """Zero/null/unparseable ranges provide no evidence, rather than a match."""
    try:
        a, b = int(str(low)), int(str(high))
    except (ValueError, TypeError):
        return None
    return (min(a,b), max(a,b)) if a > 0 and b > 0 else None


def block_range_overlap(number, attributes):
    """Compare the anonymized hundred block, not a claimed exact house address.

    Expand only numbers ending in 00 to [N,N+99]. Other source numbers are
    checked literally. Parity is intentionally unused for an anonymized block.
    None means neither side supplies a usable positive numeric range.
    """
    number = int(number)
    upper = number+99 if number % 100 == 0 else number
    ranges = [numeric_range(attributes.get(side+'lowaddr'), attributes.get(side+'highaddr'))
              for side in ('l','r')]
    known = [r for r in ranges if r is not None]
    return any(lo <= upper and hi >= number for lo,hi in known) if known else None


def road_names(attributes):
    names = [attributes.get('rd30full'), attributes.get('rd20full')]
    for prefix in ('rd30', 'rd20'):
        names.append(' '.join(str(attributes.get(prefix+part) or '')
                              for part in ('pred','name','sfx','postd')))
    return {normalize_street_name(x) for x in names if x and str(x).strip()}


def assess(record, roads, *, complete=True, threshold=REVIEW_DISTANCE_M):
    """Independent assessment with explicit reasons and compatible-road evidence.

    50 m is a conservative review trigger for block interpolation / roadway
    offsets, not an empirically calibrated accuracy guarantee. More than one
    compatible segment within that distance is flagged for review, even at a
    shared endpoint. Missing road coverage is never a geographical rejection.
    """
    if not math.isfinite(threshold) or threshold <= 0:
        raise ValueError('threshold must be finite and positive')
    result = dict(assessment='unverified', reasons=[], nearest_roadsegid=None,
                  distance_m=None, name_candidates=0, range_candidates=0,
                  plausible_candidates=0, compatible_segments=[], threshold_m=threshold)
    reasons = result['reasons']
    if record['geocode_status'] != 'matched':
        reasons.append('No provider-accepted coordinate to validate')
        return result
    lng, lat = record.get('longitude'), record.get('latitude')
    if (not isinstance(lng,(float,int)) or not isinstance(lat,(float,int))
            or not math.isfinite(lng) or not math.isfinite(lat)
            or not -180 <= lng <= 180 or not -90 <= lat <= 90):
        result.update(assessment='rejected', reasons=['Invalid coordinate'])
        return result
    # A deliberately broad screen, not a municipal boundary or location proof.
    if not (-117.65 <= lng <= -116.80 and 32.50 <= lat <= 33.20):
        result.update(assessment='rejected', reasons=['Outside broad San Diego screening envelope'])
        return result
    components = record.get('returned_components') or {}
    returned = ' '.join(str(components.get(k) or '') for k in
                        ('preDirection','streetName','suffixType','suffixDirection'))
    expected = normalize_street_name(record['primary_street'])
    if (normalize_street_name(returned) != expected or components.get('state') != 'CA'
            or str(components.get('city','')).upper() not in {'SAN DIEGO','LA JOLLA'}):
        result.update(assessment='rejected', reasons=['Returned street/direction/suffix/locality differs'])
        return result
    number = int(record['block_number'])
    census_range = numeric_range(components.get('fromAddress'), components.get('toAddress'))
    if census_range is None:
        reasons.append('Census numeric address range unavailable')
    elif not census_range[0] <= number <= census_range[1]:
        reasons.append('Submitted number outside returned Census range')
    if not complete:
        reasons.append('SanGIS candidate retrieval incomplete')
    named = [r for r in roads if expected in road_names(r['attributes'])]
    result['name_candidates'] = len(named)
    for road in named:
        attributes = road['attributes']
        if block_range_overlap(number, attributes) is not True:
            continue
        distance = road_distance_m(lng, lat, (road.get('geometry') or {}).get('paths',[]))
        if not math.isfinite(distance):
            continue
        result['compatible_segments'].append(dict(roadsegid=attributes.get('roadsegid'),
            distance_m=round(distance,3), attributes=attributes))
    segments = sorted(result['compatible_segments'], key=lambda r:(r['distance_m'],r['roadsegid'] or 0))
    result['compatible_segments'] = segments
    result['range_candidates'] = len(segments)
    nearby = [s for s in segments if s['distance_m'] <= threshold]
    result['plausible_candidates'] = len(nearby)
    if not segments:
        reasons.append('No independent name-and-block-range-compatible road geometry')
        return result
    nearest = segments[0]
    result.update(nearest_roadsegid=nearest['roadsegid'], distance_m=nearest['distance_m'])
    if nearest['distance_m'] > threshold:
        reasons.append('Distance exceeds diagnostic review threshold')
    if len(nearby) > 1:
        reasons.append('Multiple nearby name-and-block-range-compatible segments')
    a = nearest['attributes']
    if a.get('segstat') not in {'C','M'} or a.get('pending') != 'N' or a.get('dedstat') == 'A':
        reasons.append('Nearest road may be unconstructed, pending, or abandoned')
    if 'SD' not in {a.get('ljurisdic'), a.get('rjurisdic')}:
        reasons.append('Nearest road does not carry observed SD jurisdiction code')
    result['assessment'] = 'needs_review' if reasons else 'plausible'
    return result
