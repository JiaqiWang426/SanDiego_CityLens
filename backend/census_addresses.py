"""Geocoder-only comparisons. Never used to recalculate stored source hashes.

Rules: existing direction/type abbreviations at street edges; CAMINO -> CAM
only at the street prefix (after an optional direction); whole ordinal tokens
04TH -> 4TH. No fuzzy spelling, missing suffix, or dropped direction matching.
"""
import math
import re

if __package__:
    from .collision_addresses import TOKENS, clean_text
else:
    from collision_addresses import TOKENS, clean_text

DIRECTIONS = {'N','S','E','W','NE','NW','SE','SW'}


def census_street(value):
    words=clean_text(value).split()
    if not words:
        return ''
    for index in (0,len(words)-1):
        token=words[index].rstrip('.')
        if TOKENS.get(token,token) in DIRECTIONS:
            words[index]=TOKENS.get(token,token)
    suffix=len(words)-2 if words[-1] in DIRECTIONS else len(words)-1
    if suffix>=0:
        token=words[suffix].rstrip('.')
        words[suffix]=TOKENS.get(token,words[suffix])
    prefix=1 if words[0] in DIRECTIONS else 0
    if prefix<len(words)-1 and words[prefix] in {'CAMINO','CAM','CAM.'}:
        words[prefix]='CAM'
    for index,word in enumerate(words):
        ordinal=re.fullmatch(r'0*([0-9]+)(ST|ND|RD|TH)',word)
        if ordinal:
            words[index]=str(int(ordinal[1]))+ordinal[2]
    return ' '.join(words)


def supported(address):
    return (address.block_number.isdigit() and int(address.block_number)>0
            and bool(address.primary_street) and not address.intersecting_street)


def match_reason(number,street,city,state,longitude,latitude,expected_number,expected_street):
    """None means basic consistency only, never exact positional accuracy."""
    if not str(number).isdigit() or int(number)!=int(expected_number):
        return 'returned_number_differs'
    if census_street(street)!=census_street(expected_street):
        return 'returned_street_components_differ'
    if clean_text(state)!='CA' or clean_text(city) not in {'SAN DIEGO','LA JOLLA'}:
        return 'returned_locality_differs'
    if not (math.isfinite(longitude) and math.isfinite(latitude)
            and -180<=longitude<=180 and -90<=latitude<=90):
        return 'invalid_coordinates'
    # Broad admission envelope, not a verified municipal boundary polygon.
    if not (-117.6<=longitude<=-116.8 and 32.5<=latitude<=33.2):
        return 'outside_san_diego_screen'
    return None
