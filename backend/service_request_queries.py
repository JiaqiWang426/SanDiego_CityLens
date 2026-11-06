"""Read-only Python wrapper over authoritative service-request SQL aggregation."""
from datetime import datetime

if __package__:
    from .collision_queries import _run
else:
    from collision_queries import _run


def aggregate_service_requests(conn,latitude,longitude,radius_meters,start_time,end_time,
        *,exclude_linked_duplicates=False,group_by='service_name',service_name=None,status=None,service_names=None):
    """Return every group with canonical labels and distinct request-ID counts.

    NULL grouping values mean unknown for active dimensions. A filter of None
    disables filtering; a blank filter selects unknown. No synonyms are merged.
    service_names selects a union of stored labels through the additive overload;
    it is mutually exclusive with service_name. Repeated labels do not add counts.
    Equivalent unfiltered group totals equal count_service_requests. The caller
    supplies an idle dedicated connection; SQL validates all numeric arguments.
    """
    times=[datetime.fromisoformat(t) if isinstance(t,str) else t for t in (start_time,end_time)]
    if any(not isinstance(t,datetime) or t.utcoffset() is None for t in times):
        raise ValueError('Query timestamps must include UTC offsets')
    params=(latitude,longitude,radius_meters,*times,exclude_linked_duplicates,group_by,service_name,status)
    if service_names is not None:
        params=(*params,service_names)
    return _run(conn,'SELECT * FROM citylens.aggregate_service_requests('+','.join(['%s']*len(params))+')',params)
