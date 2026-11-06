"""Read-only tool-versus-direct-SQL checks against the configured reader DB."""
import argparse
from datetime import timedelta
import json
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from backend.agent.evidence import make_evidence, record_claim, verify_claims
from backend.agent.settings import Settings
from backend.agent.tools import DatabaseTools, ToolCall


def direct_groups(conn, call):
    selection = call.service_request_aggregation
    # Independent SELECT over source records; do not call production query or
    # category-normalization functions in this reference calculation.
    service = "nullif(upper(btrim(regexp_replace(r.service_name,'\\s+',' ','g'))),'')"
    status = "nullif(upper(btrim(regexp_replace(r.status,'\\s+',' ','g'))),'')"
    where = ['r.requested_at>=%s', 'r.requested_at<%s',
             'ST_DWithin(r.location,ST_SetSRID(ST_MakePoint(%s,%s),4326)::geography,%s)']
    args = [call.time_range.start,call.time_range.end,call.location.longitude,call.location.latitude,call.radius_meters]
    if call.exclude_linked_duplicates:
        where.append('r.parent_id IS NULL')
    canonical = lambda value: ' '.join(value.upper().split()) or None
    if selection.service_name is not None:
        where.append(service+' IS NOT DISTINCT FROM %s');args.append(canonical(selection.service_name))
    if selection.service_names is not None:
        where.append('EXISTS (SELECT 1 FROM unnest(%s::text[]) names(value) WHERE '+service+' IS NOT DISTINCT FROM names.value)')
        args.append(list(dict.fromkeys(canonical(name) for name in selection.service_names)))
    if selection.status is not None:
        where.append(status+' IS NOT DISTINCT FROM %s');args.append(canonical(selection.status))
    service_column = service if selection.group_by in ('service_name','both') else 'NULL::text'
    status_column = status if selection.group_by in ('status','both') else 'NULL::text'
    query = ('SELECT '+service_column+' AS service_name,'+status_column+' AS status,'
             'count(DISTINCT r.service_request_id) AS request_id_count FROM citylens.service_requests r WHERE '+
             ' AND '.join(where)+' GROUP BY 1,2')
    with conn.cursor(row_factory=dict_row) as cursor:
        cursor.execute(query,args)
        return cursor.fetchall()


def audit():
    settings=Settings.from_env()
    tools=DatabaseTools(settings.database_url)
    cases=[]
    for location in ({'id':'A','latitude':32.84,'longitude':-117.25}, {'id':'B','latitude':32.85,'longitude':-117.24}):
        base=dict(name='aggregate_service_requests',location=location,radius_meters=800,
            time_range={'start':'2026-01-01T00:00:00-08:00','end':'2026-03-01T00:00:00-08:00'},period='month',limit=100)
        for name,selection,exclude in (
            ('all_by_service',{'group_by':'service_name'},False),
            ('all_by_status',{'group_by':'status'},False),
            ('all_by_both',{'group_by':'both'},False),
            ('pothole',{'group_by':'both','service_name':' pothole '},False),
            ('road_surface',{'group_by':'both','service_names':['Pothole','Pavement Maintenance']},False),
            ('road_surface_by_status',{'group_by':'status','service_names':['Pothole','Pavement Maintenance']},False),
            ('road_surface_exclude_parent',{'group_by':'both','service_names':['Pothole','Pavement Maintenance']},True),
            ('repeated_label',{'group_by':'both','service_names':['Pothole',' POTHOLE ','Pavement Maintenance']},False),
            ('closed_road_surface',{'group_by':'both','service_names':['Pothole','Pavement Maintenance'],'status':' closed '},False),
        ):
            cases.append((location['id']+'_'+name,ToolCall(**base,exclude_linked_duplicates=exclude,service_request_aggregation=selection)))
        wide={**base,'radius_meters':50000,'time_range':{'start':'2017-01-01T00:00:00-08:00','end':'2026-11-01T00:00:00-07:00'}}
        cases.append((location['id']+'_unknown_category',ToolCall(**wide,service_request_aggregation={'group_by':'both','service_names':['']})))
    # Real record at an exact timestamp establishes inclusion/exclusion without
    # inserting or updating a boundary fixture in the development dataset.
    with psycopg.connect(settings.database_url) as conn:
        conn.execute('SET TRANSACTION READ ONLY')
        date=conn.execute('''SELECT requested_at FROM citylens.service_requests WHERE
            ST_DWithin(location,ST_SetSRID(ST_MakePoint(-117.25,32.84),4326)::geography,800)
            ORDER BY requested_at LIMIT 1''').fetchone()[0]
    base=cases[0][1].model_dump()
    for name,start,end in [('inclusive_start',date,date+timedelta(microseconds=1)),
                           ('exclusive_end',date-timedelta(microseconds=1),date)]:
        cases.append((name,ToolCall.model_validate({**base,'time_range':{'start':start,'end':end}})))
    results=[]
    for name,call in cases:
        actual=tools.execute(call)
        if actual.status!='ok':
            raise RuntimeError(name+': '+str(actual.error_code))
        with psycopg.connect(settings.database_url) as conn:
            conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            expected=direct_groups(conn,call)
        index=lambda rows: {(row['service_name'],row['status']):row['request_id_count'] for row in rows}
        assert index(actual.rows)==index(expected),name
        evidence=make_evidence(actual)
        verify_claims([record_claim(evidence)],[evidence])
        assert evidence.query_id==actual.query_id and evidence.value==sum(row['request_id_count'] for row in expected)
        results.append({'case':name,'scope':call.model_dump(mode='json'),'count':evidence.value,
            'groups':actual.rows,'query_id':actual.query_id,'evidence_id':evidence.evidence_id,
            'direct_sql_matches':True,'claims_verified':True})
    return results


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',required=True,type=Path);args=parser.parse_args()
    try:
        result=audit()
    except Exception as exc:
        raise SystemExit('Read-only aggregation audit failed: '+type(exc).__name__) from None
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,default=str,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'cases_passed':len(result),'count_summary':{row['case']:row['count'] for row in result}}))
