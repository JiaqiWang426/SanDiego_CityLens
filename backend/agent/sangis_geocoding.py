"""SanGIS SITUS Address Points fallback; no GIS download or database writes."""
from collections import Counter
import math
from urllib.parse import urlencode
from uuid import uuid4

from pydantic import ValidationError

from ..census_addresses import census_street
from .geocoding_http import get_json,GeocodingRequestError
from .location_models import GeocodedCandidate
from .models import Location
from .user_geocoding import AddressResolution,UserAddressGeocoder,interactive_street

LAYER='https://geo.sandag.org/server/rest/services/Hosted/Address_Points/FeatureServer/0'
PROVIDER='SanGIS Address Points'
# Verified against archived live layer domains, not inferred from coordinates.
PLACEMENTS={'S':('structure','Structure'),'E':('entrance','Entrance'),
            'C':('parcel_centroid','Centroid'),'U':('unknown','Unknown'),'R':('review_required','Review')}
SOURCES={'S':'SanGIS','M':'Assessor Master Property Record'}
FIELDS='objectid,addrnmbr,addrfrac,addrpdir,addrname,addrsfx,addrpostd,addrunit,addrzip,community,addrjur,state,placement_location,asource,apn,roadsegid'


def query_parameters(address):
    if not address.house_number or not address.house_number.isdigit() or int(address.house_number)<=0 or not address.street_name:
        raise ValueError('A numbered complete street is required')
    names={address.street_name.strip().upper(),census_street(address.street_name)}
    # Prefix abbreviation only; no spelling/fuzzy search or suffix omission.
    for name in list(names):
        if name.startswith('CAM '):names.add('CAMINO '+name[4:])
        if name.startswith('CLL '):names.add('CALLE '+name[4:])
    quoted=','.join("'"+name.replace("'","''")+"'" for name in sorted(names))
    return dict(where=f'addrnmbr={int(address.house_number)} AND UPPER(addrname) IN ({quoted})',
        outFields=FIELDS,outSR=4326,returnGeometry='true',resultRecordCount=200,f='json')


class SanGISAddressGeocoder:
    def resolve(self,address):
        try:
            params=query_parameters(address)
        except ValueError:
            return AddressResolution('unsupported',[],dict(provider=PROVIDER))
        try:
            payload,metadata=get_json(LAYER+'/query?'+urlencode(params),timeout=15,attempts=2)
        except GeocodingRequestError as exc:
            return AddressResolution('provider_error',[],dict(provider=PROVIDER,**exc.metadata))
        resolution=self.parse_response(address,payload)
        resolution.metadata.update(metadata)
        return resolution

    def parse_response(self,address,payload):
        info=dict(provider=PROVIDER,layer=LAYER,rejections={},raw_candidate_count=0)
        if not isinstance(payload,dict):return AddressResolution('invalid_or_out_of_scope',[],dict(info,reason='malformed_response'))
        if 'error' in payload:return AddressResolution('provider_error',[],dict(info,reason='arcgis_error',arcgis_code=payload['error'].get('code') if isinstance(payload['error'],dict) else None))
        spatial_reference=payload.get('spatialReference')
        if not isinstance(spatial_reference,dict) or spatial_reference.get('wkid')!=4326:
            return AddressResolution('invalid_or_out_of_scope',[],dict(info,reason='unconfirmed_wgs84'))
        features=payload.get('features')
        if not isinstance(features,list):return AddressResolution('invalid_or_out_of_scope',[],dict(info,reason='malformed_features'))
        info['raw_candidate_count']=len(features)
        if payload.get('exceededTransferLimit'):
            return AddressResolution('invalid_or_out_of_scope',[],dict(info,reason='incomplete_candidate_set'))
        rejected=Counter();groups={}
        for feature in features:
            try:
                if not isinstance(feature,dict) or not isinstance(feature.get('attributes'),dict):
                    rejected['malformed_feature']+=1;continue
                a=feature['attributes'];geometry=feature.get('geometry') or {}
                if not isinstance(geometry,dict):
                    rejected['missing_or_invalid_geometry']+=1;continue
                if isinstance(a.get('addrnmbr'),bool):
                    rejected['house_number']+=1;continue
                number=float(a['addrnmbr'])
                street=' '.join(str(a.get(k) or '').strip() for k in ['addrpdir','addrname','addrsfx','addrpostd'])
                if not math.isfinite(number) or not number.is_integer() or int(number)!=int(address.house_number):
                    rejected['house_number']+=1;continue
                if a.get('addrfrac') and str(a['addrfrac']).strip():
                    rejected['house_number_fraction']+=1;continue
                if interactive_street(street)!=interactive_street(address.street):
                    expected_direction=census_street(address.direction or '')
                    actual_direction=census_street(a.get('addrpdir') or '')
                    if (expected_direction!=actual_direction or
                            census_street(address.post_direction or '')!=census_street(a.get('addrpostd') or '')):
                        rejected['direction_components']+=1
                    rejected['street_components']+=1;continue
                if a.get('addrjur')!='SD' or a.get('state')!='CA':
                    rejected['jurisdiction_or_state']+=1;continue
                if address.postal_code and str(a.get('addrzip') or '')!=address.postal_code[:5]:
                    rejected['zip_missing' if not a.get('addrzip') else 'zip_conflict']+=1;continue
                x,y=geometry.get('x'),geometry.get('y')
                if isinstance(x,bool) or isinstance(y,bool) or not isinstance(x,(int,float)) or not isinstance(y,(int,float)) or not math.isfinite(x) or not math.isfinite(y):
                    rejected['missing_or_invalid_geometry']+=1;continue
                location=Location(id=address.id,latitude=y,longitude=x)
                unit=str(a.get('addrunit') or '').strip()
                # Collapse only exact coincident points for the same full address,
                # ZIP and unit. No distance tolerance, preferred APN or first row.
                key=(int(number),interactive_street(street),str(a.get('addrzip') or ''),unit,x,y)
                groups.setdefault(key,[]).append(dict(attributes=a,location=location,street=street))
            except (TypeError,KeyError,ValueError,ValidationError):
                rejected['malformed_or_out_of_scope']+=1
        candidates=[]
        for key,records in sorted(groups.items()):
            attributes=[r['attributes'] for r in records]
            placements={a.get('placement_location') for a in attributes}
            precision=PLACEMENTS.get(next(iter(placements)),('unknown','Undocumented'))[0] if len(placements)==1 else 'mixed_placement'
            conflict=any(a.get('asource')=='M' and a.get('placement_location')!='C' for a in attributes)
            if conflict:precision='unknown'
            number,street,zip_code,unit,x,y=key
            formatted=f'{number} {street}'+(f' #{unit}' if unit else '')+', '+(address.city or 'San Diego')+', CA'+(' '+zip_code if zip_code else '')
            candidates.append(GeocodedCandidate(candidate_id='candidate_'+uuid4().hex,matched_address=formatted,
                location=Location(id=address.id,label=formatted,longitude=x,latitude=y),provider=PROVIDER,
                precision=precision,requires_confirmation=len(groups)>1 or bool(unit) or conflict,
                metadata=dict(placement_location=sorted(placements,key=lambda v:str(v)),
                    placement_labels=[PLACEMENTS.get(v,('unknown','Undocumented'))[1] for v in sorted(placements,key=lambda v:str(v))],
                    asource=sorted({str(a.get('asource')) for a in attributes}),
                    source_labels=sorted({SOURCES.get(a.get('asource'),'Undocumented') for a in attributes}),
                    source_features=attributes,feature_count=len(records),srid=4326,
                    placement_source_conflict=conflict,zip_missing=not bool(zip_code),
                    locality_basis='SD jurisdiction; community is not assumed to be postal city',
                    precision_basis='SITUS property address point; placement label is not verified rooftop or apartment-unit accuracy')))
        info['rejections']=dict(rejected)
        info['distinct_candidate_count']=len(candidates)
        if not candidates:return AddressResolution('no_match' if not features else 'invalid_or_out_of_scope',[],info)
        return AddressResolution('resolved' if len(candidates)==1 and not candidates[0].requires_confirmation else 'needs_confirmation',candidates,info)


class FallbackAddressGeocoder:
    """Census stays primary. Only a legitimate empty Census match set falls back."""
    def __init__(self,primary=None,fallback=None):
        self.primary=primary or UserAddressGeocoder()
        self.fallback=fallback or SanGISAddressGeocoder()

    def resolve(self,address):
        first=self.primary.resolve(address)
        if first.status!='no_match':return first
        result=self.fallback.resolve(address)
        chain=[dict(provider='US Census Geocoder',status=first.status),dict(provider=PROVIDER,status=result.status)]
        result.metadata['provider_attempts']=chain
        for candidate in result.candidates:candidate.metadata['provider_attempts']=chain
        return result
