"""Transient Google Places discovery. No coordinate field is requested or kept."""
import json
import threading
import time
from urllib.request import Request, urlopen
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Response
from pydantic import Field
from .models import Model
from .settings import Settings

router=APIRouter(prefix='/api/v1/locations',tags=['User location discovery'])


class SearchRequest(Model):
    text: str=Field(min_length=2,max_length=240)


class PlacesDiscovery:
    def __init__(self):
        self.tokens={}
        self.calls=[]
        self.lock=threading.Lock()

    def search(self,text,key):
        now=time.monotonic()
        with self.lock:
            self.calls=[stamp for stamp in self.calls if now-stamp<60]
            self.tokens={token:value for token,value in self.tokens.items() if now-value[0]<300}
            if len(self.calls)>=20 or len(self.tokens)>=200:
                raise HTTPException(429,'Location discovery rate limit reached. Try again later.')
            self.calls.append(now)
        # A location bias is not a restriction; Google honors explicit locality.
        body=dict(textQuery=text,pageSize=5,locationBias={'circle':{'center':{'latitude':32.85,'longitude':-117.23},'radius':35000}})
        request=Request('https://places.googleapis.com/v1/places:searchText',data=json.dumps(body).encode(),method='POST',
            headers={'Content-Type':'application/json','X-Goog-Api-Key':key,
                     'X-Goog-FieldMask':'places.id,places.displayName,places.formattedAddress,places.googleMapsUri,places.attributions'})
        try:
            with urlopen(request,timeout=10) as response:
                raw=response.read(1_000_001)
            if len(raw)>1_000_000:raise ValueError('Response budget exceeded')
            payload=json.loads(raw)
        except Exception:
            raise HTTPException(503,'Google Places discovery is unavailable. Use an independently sourced address or select a point.') from None
        return self.present(payload,text)

    def present(self,payload,original_text=''):
        candidates=[]
        for row in payload.get('places',[])[:5]:
            if not isinstance(row,dict) or not isinstance(row.get('id'),str):continue
            candidates.append(dict(place_id=row['id'],name=(row.get('displayName') or {}).get('text',''),
                formatted_address=row.get('formattedAddress',''),google_maps_uri=row.get('googleMapsUri'),
                attributions=row.get('attributions',[]),source='Google Maps',precision='discovery_only_no_analytical_coordinates'))
        token=uuid4().hex
        with self.lock:self.tokens[token]=(time.monotonic(),{row['place_id'] for row in candidates},original_text.casefold())
        return dict(search_token=token,candidates=candidates,coordinate_policy='Independent coordinates or an independently selected point are required. Google addresses and coordinates are not forwarded to analysis or saved.')

    def validate_choice(self,token,place_id,original_text):
        with self.lock:
            stamp,ids,text=self.tokens.get(token,(0,set(),''))
            return time.monotonic()-stamp<300 and place_id in ids and text==original_text.casefold()


discovery=PlacesDiscovery()


@router.post('/search')
def search(request:SearchRequest,response:Response):
    response.headers['Cache-Control']='no-store'
    key=Settings.from_env().google_places_api_key
    if not key:raise HTTPException(503,'Google Places is not configured. Use an independent address or select a point.')
    return discovery.search(request.text,key)
