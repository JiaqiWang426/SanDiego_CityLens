"""User-input resolution only. Never reads or writes collision geocodes."""
import json
import math
import re
import threading
import time
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen
from uuid import uuid4

from .location_models import GeocodedCandidate
from .models import Location
from .sangis_geocoding import FallbackAddressGeocoder
from .user_geocoding import AddressResolution

COORDINATES = re.compile(r'^\s*\(?\s*(-?\d{1,3}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)\s*\)?\s*$')
NUMBER_WORDS = {'one':1,'two':2,'three':3,'four':4,'five':5,'six':6,'seven':7,'eight':8,'nine':9,
    'ten':10,'eleven':11,'twelve':12,'thirteen':13,'fourteen':14,'fifteen':15,'sixteen':16,
    'seventeen':17,'eighteen':18,'nineteen':19,'twenty':20,'thirty':30,'forty':40,'fifty':50,
    'sixty':60,'seventy':70,'eighty':80,'ninety':90}


def normalized_number_quote(quote):
    """Only called for explicitly classified street addresses, never POI names."""
    match=re.match(r'^\s*([A-Za-z]+)(?:[- ]([A-Za-z]+))?\b',quote)
    if not match or match[1].lower() not in NUMBER_WORDS:
        return quote
    number=NUMBER_WORDS[match[1].lower()]
    end=match.end(1)
    if number >= 20 and match[2] and match[2].lower() in NUMBER_WORDS and NUMBER_WORDS[match[2].lower()] < 10:
        number+=NUMBER_WORDS[match[2].lower()];end=match.end(2)
    return str(number)+quote[end:]


def coordinate_candidate(address):
    match=COORDINATES.fullmatch(address.source_quote)
    if not match:
        raise ValueError('User coordinates must be an exact quoted latitude, longitude pair.')
    point=Location(id=address.id,label=address.source_quote,latitude=float(match[1]),longitude=float(match[2]))
    return GeocodedCandidate(candidate_id='user_'+uuid4().hex,matched_address=address.source_quote,
        location=point,provider='User coordinates',precision='user_supplied',
        metadata={'coordinate_source':'user_coordinates','user_confirmed':True,'original_text':address.source_quote})


def analytical_candidate(candidate):
    """Fail closed against Google coordinates disguised as an independent pin."""
    text=' '.join(str(x) for x in [candidate.provider,candidate.metadata.get('coordinate_source','')]).lower()
    if 'google' in text or candidate.metadata.get('derived_from_google') or candidate.metadata.get('google_coordinates'):
        raise ValueError('Google discovery coordinates are not analytical coordinates.')
    return GeocodedCandidate.model_validate(candidate.model_dump())


class IndependentPOISearch:
    """Opt-in self-hosted/licensed Nominatim-compatible source; no public default.

    Search uses original user text, never Google's addresses or coordinates.
    Public OSMF Nominatim is intentionally not enabled by generated code.
    """
    _lock=threading.Lock()
    _last_request=0.0

    def __init__(self,settings):
        self.settings=settings

    def resolve(self,address):
        endpoint=self.settings.independent_poi_url
        if not endpoint or not self.settings.independent_poi_attribution or not self.settings.independent_poi_user_agent:
            return AddressResolution('no_match',[],{'provider':'Independent POI source','reason':'independent_search_not_configured'})
        if urlsplit(endpoint).hostname == 'nominatim.openstreetmap.org':
            return AddressResolution('unsupported',[],{'reason':'public_nominatim_requires_separate_informed_provider_decision'})
        params=dict(q=address.location_text or address.source_quote,format='jsonv2',addressdetails=1,limit=5,
                    viewbox='-117.6,33.2,-116.8,32.5',bounded=0)
        try:
            with self._lock:
                remaining=1.05-(time.monotonic()-self._last_request)
                if remaining>0:time.sleep(remaining)
                type(self)._last_request=time.monotonic()
                request=Request(endpoint+'?'+urlencode(params),headers={'User-Agent':self.settings.independent_poi_user_agent})
                with urlopen(request,timeout=10) as response:
                    body=response.read(1_000_001)
                if len(body)>1_000_000:raise ValueError('Provider response exceeds budget')
                payload=json.loads(body)
            return self.parse(address,payload)
        except Exception:
            return AddressResolution('provider_error',[],{'provider':'Independent POI source'})

    def parse(self,address,payload):
        candidates=[]
        for row in payload[:5] if isinstance(payload,list) else []:
            try:
                latitude,longitude=float(row['lat']),float(row['lon'])
                if not all(map(math.isfinite,[latitude,longitude])):continue
                location=Location(id=address.id,label=row['display_name'],latitude=latitude,longitude=longitude)
                candidates.append(GeocodedCandidate(candidate_id='independent_'+uuid4().hex,
                    matched_address=row['display_name'],location=location,provider='Independent POI source',
                    precision='region_representative_point' if address.entity_type=='region' else 'poi_point',requires_confirmation=True,
                    metadata={'coordinate_source':'independent_poi','original_text':address.source_quote,
                        'source_record_id':str(row.get('osm_type',''))+':'+str(row.get('osm_id','')),
                        'attribution':self.settings.independent_poi_attribution,'user_confirmed':False}))
            except (KeyError,TypeError,ValueError):continue
        return AddressResolution('needs_confirmation' if candidates else 'no_match',candidates,{'provider':'Independent POI source'})


STREET_SUFFIXES={'ST','STREET','RD','ROAD','AVE','AVENUE','AV','DR','DRIVE','BLVD','BOULEVARD','WAY','LN','LANE',
    'CT','COURT','PL','PLACE','CIR','CIRCLE','TER','TERRACE','PKWY','PKY','PARKWAY','HWY','HIGHWAY','TRL','TRAIL',
    'SQ','SQUARE','LOOP','ROW','WALK','PLZ','PLAZA','CV','COVE','PT','POINT','MALL','GLN','GLEN','XING','CRES'}
DIRECTION_WORDS={'N','S','E','W','NE','NW','SE','SW','NORTH','SOUTH','EAST','WEST'}


def spelled_address_interpretation(address):
    """Derive "1 Miramar Street" from the USER's own text "One Miramar Street
    Apartments". Never uses provider (e.g. Google) content. The result is only
    a hypothesis: every candidate it yields requires explicit confirmation."""
    text=address.location_text or address.source_quote
    normalized=normalized_number_quote(text)
    if normalized==text:
        return None
    tokens=normalized.replace(',',' ').split()
    if len(tokens)<3 or not re.fullmatch(r'[1-9]\d{0,6}',tokens[0]):
        return None
    suffix_at=next((i for i in range(2,len(tokens)) if tokens[i].upper().rstrip('.') in STREET_SUFFIXES),None)
    if suffix_at is None:
        return None
    name=tokens[1:suffix_at]
    direction=None
    if len(name)>1 and name[0].upper() in DIRECTION_WORDS:
        direction,name=name[0],name[1:]
    from .location_models import AddressProposal
    return AddressProposal(id=address.id,source_quote=address.source_quote,house_number=tokens[0],
        street_name=' '.join(name),street_suffix=tokens[suffix_at].rstrip('.'),direction=direction,
        city=address.city or 'San Diego',state=address.state or 'CA',entity_type='street_address',
        location_text=' '.join(tokens[:suffix_at+1]))


class UserLocationResolver:
    def __init__(self,settings,address_resolver=None,poi_resolver=None):
        self.address_resolver=address_resolver or FallbackAddressGeocoder()
        self.poi_resolver=poi_resolver or IndependentPOISearch(settings)

    def resolve(self,address):
        if address.entity_type=='coordinates':
            return AddressResolution('resolved',[coordinate_candidate(address)],{'provider':'User coordinates'})
        if address.entity_type=='street_address':
            first=self.address_resolver.resolve(address)
            if first.status not in {'no_match','invalid_or_out_of_scope'}:
                return first
        poi=self.poi_resolver.resolve(address)
        derived=spelled_address_interpretation(address) if address.entity_type=='building_or_poi' else None
        if derived is None:
            return poi
        try:
            street=self.address_resolver.resolve(derived)
        except Exception:
            return poi
        for candidate in street.candidates:
            # A building name is not proof of a street address: always confirm.
            candidate.requires_confirmation=True
            candidate.mismatch_reason=candidate.mismatch_reason or 'interpreted_from_building_name'
            candidate.metadata={**candidate.metadata,'interpretation':'spelled_number_street_address',
                'derived_from':'user_text','derived_address':derived.location_text,'original_text':address.source_quote}
        candidates=poi.candidates+street.candidates
        metadata={**poi.metadata,'street_interpretation':derived.location_text,
                  'street_interpretation_status':street.status,'street_provider':street.metadata.get('provider')}
        if candidates:
            return AddressResolution('needs_confirmation',candidates,metadata)
        return AddressResolution(poi.status,[],metadata)
