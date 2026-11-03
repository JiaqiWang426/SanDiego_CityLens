"""Read-only interactive address resolution; never persists collision geocodes."""
from dataclasses import dataclass,field
import math
from uuid import uuid4
from urllib.parse import urlencode

from pydantic import ValidationError

from ..collision_addresses import CollisionAddress
from ..collision_geocoding import CensusGeocoder
from ..census_addresses import match_reason
from .location_models import AddressProposal,GeocodedCandidate
from .models import Location
from .geocoding_http import get_json,GeocodingRequestError


def interactive_street(value):
    """Exact USPS token aliases, only for interactive matching (not ingestion).

    Pub 28 Appendix H: CALLE/CLL. Appendix C1: PKY/PKWY and
    PLAZA/PLZA/PLZ. Keep all names, numbers, qualifiers and directions.
    https://pe.usps.com/text/pub28/28aph.htm
    https://pe.usps.com/text/pub28/28apc_002.htm
    """
    from ..census_addresses import census_street
    words=census_street(value).split()
    if not words:return ''
    directions={'N','S','E','W','NE','NW','SE','SW'}
    prefix=1 if words[0] in directions else 0
    if prefix<len(words)-1 and words[prefix]=='CALLE':words[prefix]='CLL'
    suffix=len(words)-2 if words[-1] in directions else len(words)-1
    if suffix>=0:words[suffix]={'PKY':'PKWY','PLAZA':'PLZ','PLZA':'PLZ'}.get(words[suffix],words[suffix])
    return ' '.join(words)


@dataclass
class AddressResolution:
    status: str
    candidates: list[GeocodedCandidate]
    metadata: dict=field(default_factory=dict)


class UserAddressGeocoder:
    """Reuse Census transport/evidence, with interactive precision and choices.

    A materially different in-scope candidate is offered for explicit review.
    Multiple candidates are never selected implicitly. Out-of-scope/invalid
    coordinates are never offered. The original collision pipeline is untouched.
    """
    def __init__(self,provider=None):
        self.provider=provider or CensusGeocoder(timeout=15)
        self.custom_provider=provider is not None

    def resolve(self,address: AddressProposal) -> AddressResolution:
        source=CollisionAddress(address.house_number,address.street,'',address.display,'interactive_only')
        if self.custom_provider:
            result=self.provider.geocode(source)
            if result.status in {'error','unsupported'}:
                return AddressResolution('provider_error' if result.status=='error' else 'unsupported',[])
            return self.parse_response(address,(result.details or {}).get('response',{}))
        # Preserve supplied locality/ZIP for interactive inputs. The historical
        # collision adapter and its hardcoded block-address requests are unchanged.
        from ..census_addresses import census_street
        params=dict(street=address.house_number+' '+census_street(address.street),
            city=address.city or 'San Diego',state=address.state or 'CA',benchmark=self.provider.benchmark,format='json')
        if address.postal_code:params['zip']=address.postal_code
        try:
            payload,metadata=get_json(self.provider.endpoint+'?'+urlencode(params),attempts=1)
        except GeocodingRequestError as exc:
            return AddressResolution('provider_error',[],dict(provider='US Census Geocoder',**exc.metadata))
        resolution=self.parse_response(address,payload)
        resolution.metadata.update(metadata,provider='US Census Geocoder')
        return resolution

    def parse_response(self,address,payload):
        """Pure parser shared with the benchmark; uses existing Census validation."""
        if isinstance(payload,dict) and ('error' in payload or 'errors' in payload):
            return AddressResolution('provider_error',[],dict(provider='US Census Geocoder',reason='census_error_response'))
        source=CollisionAddress(address.house_number,address.street,'',address.display,'interactive_only')
        try:
            result=CensusGeocoder().parse_response(source,payload)
        except (TypeError,KeyError,ValueError,AttributeError):
            return AddressResolution('invalid_or_out_of_scope',[],dict(provider='US Census Geocoder',reason='malformed_or_invalid_response'))
        if result.status in {'error','unsupported'}:
            return AddressResolution('provider_error' if result.status=='error' else 'unsupported',[])
        details=result.details or {}
        response=details.get('response',{}).get('result',{})
        matches=response.get('addressMatches',[])
        if not isinstance(matches,list):
            return AddressResolution('provider_error',[])
        candidates=[]
        for match in matches:
            try:
                if not isinstance(match,dict) or not isinstance(match.get('addressComponents'),dict) or not isinstance(match.get('coordinates'),dict):
                    continue
                parts=match['addressComponents']
                longitude,latitude=float(match['coordinates']['x']),float(match['coordinates']['y'])
                if not math.isfinite(longitude) or not math.isfinite(latitude):
                    continue
                if parts.get('state')!='CA' or parts.get('city','').upper() not in {'SAN DIEGO','LA JOLLA'}:
                    continue
                location=Location(id=address.id,label=match['matchedAddress'],latitude=latitude,longitude=longitude)
                street=' '.join(str(parts.get(k) or '') for k in ('preQualifier','preDirection','preType',
                    'streetName','suffixType','suffixDirection','suffixQualifier'))
                number=match['matchedAddress'].split(' ',1)[0]
                if not number.isdigit() or int(number)<=0 or not parts.get('streetName'):
                    continue
                reason=match_reason(number,street,parts['city'],parts['state'],longitude,latitude,
                                    address.house_number,address.street)
                if reason=='returned_street_components_differ' and interactive_street(street)==interactive_street(address.street):
                    reason=None
                if address.postal_code and parts.get('zip')!=address.postal_code[:5]:
                    reason='returned_zip_missing' if not parts.get('zip') else 'returned_zip_differs'
                benchmark=response.get('input',{}).get('benchmark',{}).get('benchmarkName') or details.get('benchmark_requested')
                candidates.append(GeocodedCandidate(candidate_id='candidate_'+uuid4().hex,
                    matched_address=match['matchedAddress'],location=location,benchmark=benchmark,
                    requires_confirmation=bool(reason) or len(matches)!=1,mismatch_reason=reason,
                    metadata=dict(address_components=parts,tiger_line=match.get('tigerLine'),srid=4326,
                                  precision_basis='Census address-range interpolation; building accuracy unverified')))
            except (KeyError,TypeError,ValueError,ValidationError):
                continue
        if not candidates:
            return AddressResolution('no_match' if not matches else 'invalid_or_out_of_scope',[],
                dict(provider='US Census Geocoder',raw_candidate_count=len(matches)))
        return AddressResolution('resolved' if len(candidates)==1 and not candidates[0].requires_confirmation
                                 else 'needs_confirmation',candidates,dict(provider='US Census Geocoder',raw_candidate_count=len(matches)))
