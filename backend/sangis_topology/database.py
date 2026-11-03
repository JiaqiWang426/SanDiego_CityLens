"""Reference-only maintenance writes; separate read-only batch connections."""
import json
import os
from pathlib import Path

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pyproj import Transformer

from backend.config import load_environment
from backend.verification.intersection_feasibility.study import connection as reader_connection
from backend.verification.intersection_feasibility.topology import canonical
from .acquire import EvidenceError, layer_features, verify_files, now, save

HERE = Path(__file__).resolve().parent


def maintenance_connection(*, apply=False):
    load_environment()
    agent_url = os.environ['AGENT_DATABASE_URL']
    if apply:
        # Applying historical proposals never falls back to Agent credentials.
        url = os.environ.get('SANGIS_MAINTENANCE_DATABASE_URL')
        if not url:
            raise EvidenceError('Explicit SANGIS_MAINTENANCE_DATABASE_URL is required for apply')
    else:
        url = os.environ.get('DATABASE_URL') or make_conninfo(agent_url, user='citylens', password=os.environ['CITYLENS_DB_PASSWORD'])
    agent, maintenance = conninfo_to_dict(agent_url), conninfo_to_dict(url)
    if any(agent.get(k) != maintenance.get(k) for k in ('host', 'port', 'dbname')) or agent.get('user') == maintenance.get('user'):
        raise EvidenceError('Separate maintenance identity on the same database required')
    conn = psycopg.connect(url, connect_timeout=5, row_factory=dict_row)
    conn.execute("SET statement_timeout='120s'")
    conn.execute("SET lock_timeout='5s'")
    conn.commit()
    return conn


def geojson(feature, layer):
    g = feature.get('geometry')
    if g is None:
        return None
    if layer == 'Roads_All':
        return json.dumps({'type': 'MultiLineString', 'coordinates': g['paths']}, allow_nan=False)
    coordinates = [g['x'], g['y']]
    if 'z' in g:
        coordinates.append(g['z'])
    return json.dumps({'type': 'Point', 'coordinates': coordinates}, allow_nan=False)


def import_snapshot(directory):
    manifest = verify_files(directory)
    sid = manifest['snapshot_id']
    with maintenance_connection() as conn:
        conn.execute('SELECT pg_advisory_xact_lock(8420162)')
        conn.execute((HERE / 'reference.sql').read_text(encoding='utf-8'))
        old = conn.execute('SELECT manifest FROM citylens_ref.snapshots WHERE snapshot_id=%s', (sid,)).fetchone()
        if old:
            if old['manifest'] != manifest:
                raise EvidenceError('Existing immutable snapshot manifest differs')
            return {'snapshot_id': sid, 'action': 'already_imported'}
        conn.execute('INSERT INTO citylens_ref.snapshots(snapshot_id,manifest) VALUES(%s,%s)', (sid, Jsonb(manifest)))
        # A single transaction publishes the entire immutable snapshot or nothing.
        # COPY uses a session-local staging table, never a collision table.
        conn.execute('CREATE TEMP TABLE sangis_stage(attributes jsonb,source_geometry jsonb,geojson text) ON COMMIT DROP')
        for layer, table in [('Roads_All', 'road_segments'), ('Roads_Intersection', 'road_nodes')]:
            conn.execute('TRUNCATE sangis_stage')
            with conn.cursor().copy('COPY sangis_stage(attributes,source_geometry,geojson) FROM STDIN') as copy:
                for feature in layer_features(directory, layer, manifest):
                    attrs = dict(feature['attributes'])
                    if layer == 'Roads_All':
                        attrs['_citylens_canonical_name'] = canonical(attrs.get('rd30full') or '')
                    copy.write_row((Jsonb(attrs), Jsonb(feature.get('geometry')), geojson(feature, layer)))
            if layer == 'Roads_All':
                conn.execute('''INSERT INTO citylens_ref.road_segments
                    (snapshot_id,roadsegid,objectid,rd30full,rd30pred,rd30name,rd30sfx,rd30postd,
                     canonical_name,fnode,tnode,f_level,t_level,segclass,segstat,attributes,source_geometry,geom,geom_4326)
                    SELECT %s,(attributes->>'roadsegid')::bigint,(attributes->>'objectid')::bigint,
                     attributes->>'rd30full',attributes->>'rd30pred',attributes->>'rd30name',attributes->>'rd30sfx',attributes->>'rd30postd',
                     attributes->>'_citylens_canonical_name',(attributes->>'fnode')::bigint,(attributes->>'tnode')::bigint,
                     (attributes->>'f_level')::integer,(attributes->>'t_level')::integer,attributes->>'segclass',attributes->>'segstat',
                     attributes-'_citylens_canonical_name',source_geometry,g,ST_Transform(ST_Force2D(g),4326)
                    FROM (SELECT *,ST_SetSRID(ST_GeomFromGeoJSON(geojson),2230) AS g FROM sangis_stage) s''', (sid,))
            else:
                conn.execute('''INSERT INTO citylens_ref.road_nodes
                    (snapshot_id,interid,objectid,globalid,node_type,attributes,source_geometry,geom,geom_4326)
                    SELECT %s,(attributes->>'interid')::bigint,(attributes->>'objectid')::bigint,attributes->>'globalid',attributes->>'type',
                    attributes,source_geometry,g,ST_Transform(ST_Force2D(g),4326)
                    FROM (SELECT *,ST_SetSRID(ST_GeomFromGeoJSON(geojson),2230) AS g FROM sangis_stage) s''', (sid,))
            actual = conn.execute(f'SELECT count(*) AS n FROM citylens_ref.{table} WHERE snapshot_id=%s', (sid,)).fetchone()['n']
            if actual != manifest['layers'][layer]['feature_count']:
                raise EvidenceError('Import count mismatch')
            print(f'Imported {actual} {table}', flush=True)
        conn.execute('ANALYZE citylens_ref.road_segments')
        conn.execute('ANALYZE citylens_ref.road_nodes')
    return {'snapshot_id': sid, 'action': 'imported'}


def reference_reader_guard(conn):
    row = conn.execute('''SELECT has_schema_privilege(current_user,'citylens_ref','CREATE') AS schema_write,
         EXISTS(SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
          WHERE n.nspname='citylens_ref' AND c.relkind IN ('r','p')
          AND has_table_privilege(current_user,c.oid,'INSERT,UPDATE,DELETE,TRUNCATE,TRIGGER,REFERENCES')) AS table_write''').fetchone()
    if row['schema_write'] or row['table_write']:
        raise EvidenceError('Agent has writable reference privileges')
    return {'schema_write': False, 'table_write': False}


def validate_snapshot(sid, output):
    result = {'snapshot_id': sid, 'checked_at': now()}
    with reader_connection() as conn:
        result['agent_reference_permissions'] = reference_reader_guard(conn)
        for table in ('road_segments', 'road_nodes'):
            result[table] = conn.execute(f'''SELECT count(*) AS count,
                count(*) FILTER(WHERE geom IS NULL OR ST_IsEmpty(geom)) AS empty_geometry,
                count(*) FILTER(WHERE NOT ST_IsValid(geom)) AS invalid_geometry,
                count(*) FILTER(WHERE ST_SRID(geom)<>2230 OR ST_SRID(geom_4326)<>4326) AS invalid_srid,
                count(*) FILTER(WHERE ST_CoordDim(geom)=3) AS geometry_z_count
                FROM citylens_ref.{table} WHERE snapshot_id=%s''', (sid,)).fetchone()
        result['endpoints'] = conn.execute('''WITH endpoints AS (
             SELECT roadsegid,fnode AS node,f_level AS level,ST_StartPoint(ST_GeometryN(geom,1)) AS p FROM citylens_ref.road_segments WHERE snapshot_id=%s
             UNION ALL SELECT roadsegid,tnode,t_level,ST_EndPoint(ST_GeometryN(geom,ST_NumGeometries(geom))) FROM citylens_ref.road_segments WHERE snapshot_id=%s
            ), joined AS (SELECT e.*,n.node_records,n.distance_m FROM endpoints e LEFT JOIN LATERAL (
              SELECT count(*) AS node_records,max(ST_Distance(ST_Force2D(e.p),ST_Force2D(n.geom))*0.3048006096012192) AS distance_m
              FROM citylens_ref.road_nodes n WHERE n.snapshot_id=%s AND n.interid=e.node) n ON true)
            SELECT count(*) AS count,count(*) FILTER(WHERE node IS NULL OR node<=0) AS missing_identity,
             count(*) FILTER(WHERE node_records=0) AS dangling_references,
             count(*) FILTER(WHERE node_records>1) AS duplicate_node_references,
             count(*) FILTER(WHERE level IS NULL OR level NOT BETWEEN 0 AND 9) AS invalid_levels,
             max(distance_m) AS max_distance_m,percentile_cont(ARRAY[0.5,0.95,0.99]) WITHIN GROUP(ORDER BY distance_m) AS distance_quantiles_m,
             count(*) FILTER(WHERE distance_m>2) AS beyond_2m,
             count(*) FILTER(WHERE distance_m>0.01) AS beyond_1cm FROM joined''', (sid, sid, sid)).fetchone()
        result['duplicate_node_ids'] = conn.execute('''SELECT interid,count(*) AS records,array_agg(objectid ORDER BY objectid) AS objectids
            FROM citylens_ref.road_nodes WHERE snapshot_id=%s GROUP BY interid HAVING count(*)>1 ORDER BY interid''',(sid,)).fetchall()
        result['invalid_node_ids'] = conn.execute('SELECT count(*) AS n FROM citylens_ref.road_nodes WHERE snapshot_id=%s AND interid<=0',(sid,)).fetchone()['n']
        result['dangling_examples'] = conn.execute('''SELECT r.roadsegid,r.fnode,r.tnode,r.segclass,r.segstat FROM citylens_ref.road_segments r
            LEFT JOIN citylens_ref.road_nodes f ON f.snapshot_id=r.snapshot_id AND f.interid=r.fnode
            LEFT JOIN citylens_ref.road_nodes t ON t.snapshot_id=r.snapshot_id AND t.interid=r.tnode
            WHERE r.snapshot_id=%s AND (f.interid IS NULL OR t.interid IS NULL) ORDER BY r.roadsegid LIMIT 30''', (sid,)).fetchall()
        result['orphan_nodes'] = conn.execute('''SELECT count(*) AS n FROM citylens_ref.road_nodes n WHERE snapshot_id=%s
            AND NOT EXISTS(SELECT 1 FROM citylens_ref.road_segments r WHERE r.snapshot_id=n.snapshot_id AND (r.fnode=n.interid OR r.tnode=n.interid))''', (sid,)).fetchone()['n']
        for field, table in [('segclass','road_segments'), ('segstat','road_segments'), ('node_type','road_nodes'), ('f_level','road_segments'), ('t_level','road_segments')]:
            result[field] = conn.execute(f'SELECT {field} AS value,count(*) AS count FROM citylens_ref.{table} WHERE snapshot_id=%s GROUP BY 1 ORDER BY 1', (sid,)).fetchall()
        result['multipart_roads'] = conn.execute('SELECT count(*) AS n FROM citylens_ref.road_segments WHERE snapshot_id=%s AND ST_NumGeometries(geom)<>1', (sid,)).fetchone()['n']
        result['nonsimple_roads'] = conn.execute('SELECT count(*) AS n FROM citylens_ref.road_segments WHERE snapshot_id=%s AND NOT ST_IsSimple(geom)', (sid,)).fetchone()['n']
        # Identical alignments are source records, not dissolved or silently fixed.
        result['duplicate_geometry_groups'] = conn.execute('''SELECT count(*) AS n FROM (
          SELECT ST_AsEWKB(ST_Force2D(geom)) FROM citylens_ref.road_segments WHERE snapshot_id=%s
          GROUP BY 1 HAVING count(*)>1) s''', (sid,)).fetchone()['n']
        # Detect nonidentical shared alignments too; equal geometry alone misses
        # partial overlaps. This is a diagnostic, never an inferred connection.
        result['overlapping_segment_pairs'] = conn.execute('''SELECT count(*) AS n
          FROM citylens_ref.road_segments a JOIN citylens_ref.road_segments b
           ON b.snapshot_id=a.snapshot_id AND b.roadsegid>a.roadsegid AND a.geom && b.geom
          WHERE a.snapshot_id=%s AND ST_Relate(ST_Force2D(a.geom),ST_Force2D(b.geom),'1********')''', (sid,)).fetchone()['n']
        result['overlap_examples'] = conn.execute('''SELECT a.roadsegid AS first_id,b.roadsegid AS second_id,
           a.rd30full AS first_road,b.rd30full AS second_road,a.segstat AS first_status,b.segstat AS second_status,
           ARRAY[a.f_level,a.t_level] AS first_levels,ARRAY[b.f_level,b.t_level] AS second_levels
          FROM citylens_ref.road_segments a JOIN citylens_ref.road_segments b
           ON b.snapshot_id=a.snapshot_id AND b.roadsegid>a.roadsegid AND a.geom && b.geom
          WHERE a.snapshot_id=%s AND ST_Relate(ST_Force2D(a.geom),ST_Force2D(b.geom),'1********')
          LIMIT 25''', (sid,)).fetchall()
        result['disconnected_coincident_endpoint_groups'] = conn.execute('''WITH e AS (
          SELECT fnode AS node,ST_StartPoint(ST_GeometryN(geom,1)) AS p FROM citylens_ref.road_segments WHERE snapshot_id=%s
          UNION ALL SELECT tnode,ST_EndPoint(ST_GeometryN(geom,ST_NumGeometries(geom))) FROM citylens_ref.road_segments WHERE snapshot_id=%s)
          SELECT count(*) AS n FROM (SELECT ST_AsEWKB(ST_Force2D(p)) FROM e GROUP BY 1 HAVING count(DISTINCT node)>1) d''', (sid,sid)).fetchone()['n']
        points = conn.execute('SELECT ST_X(geom) AS x,ST_Y(geom) AS y,ST_X(geom_4326) AS lon,ST_Y(geom_4326) AS lat FROM citylens_ref.road_nodes WHERE snapshot_id=%s ORDER BY interid LIMIT 100', (sid,)).fetchall()
        transform = Transformer.from_crs(2230,4326,always_xy=True)
        result['pyproj_postgis_max_degrees'] = max(max(abs(a-b) for a,b in zip(transform.transform(p['x'],p['y']), (p['lon'],p['lat']))) for p in points)
        meta = conn.execute('SELECT manifest FROM citylens_ref.snapshots WHERE snapshot_id=%s',(sid,)).fetchone()['manifest']
        result['counts_match_manifest'] = all(result[t]['count']==meta['layers'][l]['feature_count'] for t,l in [('road_segments','Roads_All'),('road_nodes','Roads_Intersection')])
    result['usable_for_conservative_proposals'] = result['counts_match_manifest'] and all(not result[t][k] for t in ('road_segments','road_nodes') for k in ('empty_geometry','invalid_geometry','invalid_srid'))
    result['tolerance_basis'] = 'Stage A 2m endpoint consistency ceiling only, never creates links. Native XY tolerance 0.0032808333ft (~1mm); full-network discrepancy distribution reported. Exact integer node identity is mandatory. No snapping or geometry repair.'
    save(output,result)
    with maintenance_connection() as conn:
        conn.execute('UPDATE citylens_ref.snapshots SET integrity=%s,validated_at=now() WHERE snapshot_id=%s', (Jsonb(result),sid))
    return result
