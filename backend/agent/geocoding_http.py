"""Small bounded JSON GET helper for interactive public geocoders only."""
import json
import time
from urllib.error import HTTPError,URLError
from urllib.request import Request,urlopen,HTTPRedirectHandler,build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,request,fp,code,msg,headers,newurl):
        return None


class GeocodingRequestError(RuntimeError):
    def __init__(self,metadata):
        self.metadata=metadata
        super().__init__('Public geocoder request failed')


def get_json(url,*,timeout=15,attempts=2,follow_redirects=True):
    if not 0<timeout<=30 or attempts not in {1,2}:
        raise ValueError('Interactive geocoder budgets exceeded')
    started=time.monotonic()
    errors=[]
    open_request=urlopen if follow_redirects else build_opener(NoRedirect()).open
    for attempt in range(1,attempts+1):
        try:
            with open_request(Request(url,headers={'User-Agent':'CityLens/0.3 (interactive public-data resolution)'}),timeout=timeout) as response:
                data=response.read(2_000_001)
            if len(data)>2_000_000:raise ValueError('Response too large')
            payload=json.loads(data)
            return payload,dict(http_attempts=attempt,latency_ms=round((time.monotonic()-started)*1000,3),request_errors=errors)
        except (HTTPError,URLError,TimeoutError,OSError,ValueError) as exc:
            status=exc.code if isinstance(exc,HTTPError) else None
            errors.append(dict(error_type=type(exc).__name__,http_status=status))
            transient=(status in {408,429,500,502,503,504} if status is not None
                       else isinstance(exc,(URLError,TimeoutError,OSError)))
            if not transient or attempt==attempts:break
            time.sleep(.5*attempt)
    raise GeocodingRequestError(dict(http_attempts=attempt,latency_ms=round((time.monotonic()-started)*1000,3),request_errors=errors))
