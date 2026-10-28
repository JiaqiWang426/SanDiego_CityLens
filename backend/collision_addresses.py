"""Conservative, reusable normalization of block-level collision addresses.

Normalization is textual; it neither geocodes nor invents house numbers.
Unknown direction/suffix tokens survive unchanged. Raw source strings are
preserved separately by the importer.
"""
from dataclasses import dataclass
import hashlib
import json


TOKENS = {
    'STREET': 'ST', 'ROAD': 'RD', 'DRIVE': 'DR', 'AVENUE': 'AVE',
    'BOULEVARD': 'BLVD', 'COURT': 'CT', 'LANE': 'LN', 'PLACE': 'PL',
    'PARKWAY': 'PKWY', 'HIGHWAY': 'HWY', 'TERRACE': 'TER', 'CIRCLE': 'CIR',
    'NORTH': 'N', 'SOUTH': 'S', 'EAST': 'E', 'WEST': 'W',
    'NORTHEAST': 'NE', 'NORTHWEST': 'NW', 'SOUTHEAST': 'SE', 'SOUTHWEST': 'SW',
}


def clean_text(value):
    """Trim/collapse whitespace and uppercase; retain meaningful punctuation."""
    return ' '.join(str(value).upper().split()) if value is not None else ''


def normalize_street_name(value):
    """Canonical full street name for exact matching, including direction/type.

    Only outer direction and suffix tokens are canonicalized. This is a
    textual comparison convention, not verification of an official street name.
    """
    words = clean_text(value).split()
    if words:
        words[-1] = TOKENS.get(words[-1].rstrip('.'), words[-1].rstrip('.'))
    if len(words) > 1:
        direction = words[0].rstrip('.')
        if direction in {'NORTH','SOUTH','EAST','WEST','NORTHEAST','NORTHWEST','SOUTHEAST','SOUTHWEST',
                         'N','S','E','W','NE','NW','SE','SW'}:
            words[0] = TOKENS.get(direction, direction)
    return ' '.join(words)


@dataclass(frozen=True)
class CollisionAddress:
    block_number: str
    primary_street: str
    intersecting_street: str
    normalized_address: str
    address_key: str


def normalize_collision_address(record):
    """Combine primary components plus optional cross street, without guessing.

    The number is the source's anonymized block number. A cross street is
    retained as ' & CROSS STREET'; locality is explicit San Diego, CA.
    The address key includes each cleaned source component, so meaningful
    address changes invalidate prior geocoding even if the display is similar.
    """
    fields = ('ADDRESS_NO_PRIMARY', 'ADDRESS_PD_PRIMARY', 'ADDRESS_ROAD_PRIMARY',
              'ADDRESS_SFX_PRIMARY', 'ADDRESS_PD_INTERSECTING',
              'ADDRESS_NAME_INTERSECTING', 'ADDRESS_SFX_INTERSECTING')
    components = {key: clean_text(record.get(key)) for key in fields}
    def street(direction, name, suffix):
        # Do not turn a lone suffix/direction into a usable street address.
        if not components[name]:
            return ''
        return ' '.join(filter(None, [TOKENS.get(components[direction], components[direction]),
            components[name], TOKENS.get(components[suffix].rstrip('.'), components[suffix].rstrip('.'))]))
    primary = street(fields[1], fields[2], fields[3])
    cross = street(fields[4], fields[5], fields[6])
    number = components[fields[0]]
    display = ' '.join(filter(None, [number, primary]))
    if cross:
        display = f'{display} & {cross}' if display else cross
    if display:
        display += ', SAN DIEGO, CA'
    key = hashlib.sha256(json.dumps({'version': 1, **components}, sort_keys=True).encode()).hexdigest()
    return CollisionAddress(number, primary, cross, display, key)
