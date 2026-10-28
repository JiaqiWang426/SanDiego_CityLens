"""Collision normalization/unit tests and rollback-only PostGIS integration tests.

Set DATABASE_URL to exercise integration tests against the existing local DB
after db.py init. Network geocoding is mocked; no raw source file is modified.
"""
import csv
from dataclasses import replace
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import psycopg

from collision_addresses import normalize_collision_address, normalize_street_name
from collision_ingest import FIELDS, load_collisions, parse_collision
from collision_geocoding import CensusGeocoder, GeocodeResult, save_geocode
from collision_queries import _parameters


def record(**changes):
    row = dict.fromkeys(FIELDS,'')
    row.update(REPORT_ID='__citylens_test_a',DATE_TIME='1901-01-01 00:00:00.000',
               ADDRESS_NO_PRIMARY='8100',ADDRESS_ROAD_PRIMARY='LA JOLLA SCENIC',
               ADDRESS_SFX_PRIMARY='DRIVE',INJURED='0',KILLED='0')
    row.update(changes)
    return row


class CollisionUnitTests(unittest.TestCase):
    def test_normalized_address_includes_intersection_and_key(self):
        row = record(ADDRESS_PD_PRIMARY=' NORTH ',ADDRESS_NAME_INTERSECTING=' gilman ',
                     ADDRESS_SFX_INTERSECTING='DRIVE')
        address = normalize_collision_address(row)
        self.assertEqual(address.normalized_address,
            '8100 N LA JOLLA SCENIC DR & GILMAN DR, SAN DIEGO, CA')
        self.assertEqual(normalize_street_name('north La Jolla Scenic Drive'),address.primary_street)
        self.assertEqual(normalize_collision_address(dict(row,ADDRESS_NO_PRIMARY=' 8100 ')).address_key,
                         address.address_key)
        self.assertNotEqual(normalize_collision_address(dict(row,ADDRESS_NO_PRIMARY='8200')).address_key,
                            address.address_key)

    def test_invalid_and_missing_counts_never_become_zero(self):
        values = parse_collision(record(INJURED='',KILLED='-1',DATE_TIME='bad',HIT_RUN_LVL='UNKNOWN'))
        self.assertIsNone(values[1])
        self.assertEqual(values[10:12],(None,None))
        self.assertIsNone(values[13])
        self.assertEqual(len(values[-1]),4)
        self.assertEqual(parse_collision(record(INJURED='0',KILLED='2'))[10:12],(0,2))
        for bad in ['NaN','1.5','infinity','2147483648']:
            self.assertIsNone(parse_collision(record(INJURED=bad))[10])

    def test_census_requires_unambiguous_evidence(self):
        provider = CensusGeocoder()
        address = normalize_collision_address(record())
        match = dict(matchedAddress='8100 LA JOLLA SCENIC DR, SAN DIEGO, CA',
                     addressComponents=dict(streetName='LA JOLLA SCENIC',suffixType='DR',state='CA',city='SAN DIEGO'),
                     coordinates=dict(x=-117.25,y=32.84))
        payload = {'result':{'addressMatches':[match]}}
        result = provider.parse_response(address,payload)
        self.assertEqual(result.status,'matched')
        self.assertEqual(result.precision,'block_approximate')
        self.assertEqual(provider.parse_response(address,{'result':{'addressMatches':[]}}).status,'no_match')
        self.assertEqual(provider.parse_response(address,{'result':{'addressMatches':[match,match]}}).status,'ambiguous')
        wrong = dict(match,matchedAddress='8200 LA JOLLA SCENIC DR')
        self.assertEqual(provider.parse_response(address,{'result':{'addressMatches':[wrong]}}).status,'ambiguous')
        with patch('collision_geocoding.urlopen') as http:
            self.assertEqual(provider.geocode(replace(address,intersecting_street='GILMAN DR')).status,'unsupported')
            http.assert_not_called()
        with patch('collision_geocoding.urlopen',side_effect=TimeoutError('timeout')):
            result = provider.geocode(address)
            self.assertEqual(result.status,'error')
            self.assertIsNone(result.latitude)

    def test_python_query_time_and_street_validation(self):
        with self.assertRaises(ValueError):
            _parameters('1901-01-01','1901-02-01')
        params = _parameters('1901-01-01T00:00:00Z','1901-02-01T00:00:00Z',
                             street_name='La Jolla Scenic Drive')
        self.assertEqual(params[-2],'LA JOLLA SCENIC DR')


@unittest.skipUnless(os.environ.get('DATABASE_URL'),'Set DATABASE_URL for PostGIS tests')
class CollisionDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.conn = psycopg.connect(os.environ['DATABASE_URL'],connect_timeout=10)
        self.addCleanup(self.conn.close)
        self.addCleanup(self.conn.rollback)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/'collisions.csv'

    def load(self, rows):
        with self.path.open('w',encoding='utf-8',newline='') as stream:
            writer = csv.DictWriter(stream,fieldnames=[*FIELDS,'EXTRA'])
            writer.writeheader()
            writer.writerows(rows)
        return load_collisions(self.conn,self.path,batch_size=2)

    def test_archive_upsert_and_geocode_lifecycle(self):
        first = record(INJURED='',EXTRA=' preserve whitespace ')
        stats = self.load([first,record(REPORT_ID='',KILLED='bad')])
        self.assertEqual((stats['accepted_rows'],stats['rejected_rows']),(1,1))
        raw,injured,key = self.conn.execute('''SELECT raw_record,injured,address_key
            FROM citylens.traffic_collisions WHERE report_id=%s''',(first['REPORT_ID'],)).fetchone()
        self.assertIsNone(injured)
        self.assertEqual(raw['EXTRA'],' preserve whitespace ')
        matched = GeocodeResult('matched','fixture',-117.25,32.84,'block_approximate',{'fixture':True})
        self.assertTrue(save_geocode(self.conn,first['REPORT_ID'],key,matched))
        self.load([dict(first,INJURED='2')])
        injured,status = self.conn.execute('''SELECT injured,geocode_status
            FROM citylens.traffic_collisions WHERE report_id=%s''',(first['REPORT_ID'],)).fetchone()
        self.assertEqual((injured,status),(2,'matched'))
        # A changed cross street must invalidate every piece of geocoding metadata.
        self.load([dict(first,ADDRESS_NAME_INTERSECTING='GILMAN',ADDRESS_SFX_INTERSECTING='DR')])
        state = self.conn.execute('''SELECT geocode_status,location,geocode_provider,
            geocode_precision,geocoded_at,geocode_details FROM citylens.traffic_collisions
            WHERE report_id=%s''',(first['REPORT_ID'],)).fetchone()
        self.assertEqual(state,('pending',None,None,None,None,None))
        self.assertFalse(save_geocode(self.conn,first['REPORT_ID'],key,matched))
        archived = self.conn.execute('SELECT count(*) FROM citylens.collision_source_rows WHERE import_id=%s',
                                      (stats['import_id'],)).fetchone()[0]
        self.assertEqual(archived,2)
        # A later source occurrence replaces the earlier one, without duplication.
        self.load([record(INJURED='1'),record(INJURED='3')])
        self.assertEqual(self.conn.execute('SELECT injured FROM citylens.traffic_collisions WHERE report_id=%s',
                                          (first['REPORT_ID'],)).fetchone()[0],3)

    def test_queries_boundaries_unknowns_and_aggregation(self):
        self.load([record(),record(REPORT_ID='__citylens_test_b',INJURED='',KILLED='',HIT_RUN_LVL='FELONY'),
                   record(REPORT_ID='__citylens_test_end',DATE_TIME='1901-01-02 00:00:00')])
        key=self.conn.execute("SELECT address_key FROM citylens.traffic_collisions WHERE report_id='__citylens_test_a'").fetchone()[0]
        save_geocode(self.conn,'__citylens_test_a',key,
                     GeocodeResult('matched','fixture',-117.25,32.84,'block_approximate'))
        start,end='1901-01-01T00:00:00-08:00','1901-01-02T00:00:00-08:00'
        def count(**filters):
            params=_parameters(start,end,**filters)
            return self.conn.execute('SELECT count(*) FROM citylens.filter_traffic_collisions('+
                                     ','.join(['%s']*len(params))+')',params).fetchone()[0]
        self.assertEqual(count(),2)
        self.assertEqual(count(min_injured=0,max_injured=0),1)
        self.assertEqual(count(hit_run=True),1)
        self.assertEqual(count(hit_run=False),1)
        self.assertEqual(count(hit_run_level='FELONY'),1)
        self.assertEqual(count(min_killed=0,max_killed=0),1)
        self.assertEqual(count(street_name='La Jolla Scenic Drive'),2)
        self.assertEqual(count(latitude=32.84,longitude=-117.25,radius_meters=0),1)
        self.assertEqual(count(latitude=32,longitude=-117,radius_meters=800),0)
        row=self.conn.execute('SELECT * FROM citylens.aggregate_traffic_collisions(%s,%s)',(start,end)).fetchone()
        self.assertEqual(row[1:],(2,1,1,1))
        # Public retrieval is paginated deterministically and validates bounds.
        page=self.conn.execute('''SELECT report_id FROM citylens.get_traffic_collisions(
            %s,%s,result_limit=>1,result_offset=>1)''',(start,end)).fetchall()
        self.assertEqual(page,[('__citylens_test_b',)])
        with self.assertRaises(psycopg.errors.InvalidParameterValue):
            with self.conn.transaction():
                self.conn.execute('''SELECT * FROM citylens.get_traffic_collisions(
                    %s,%s,result_limit=>1001)''',(start,end))
        # Calendar grouping uses the requested timezone, not the session setting.
        self.conn.execute("SET LOCAL TIME ZONE 'Asia/Tokyo'")
        same=self.conn.execute('SELECT * FROM citylens.aggregate_traffic_collisions(%s,%s)',(start,end)).fetchone()
        self.assertEqual(row,same)
        for extra in [dict(latitude=91,longitude=-117,radius_meters=800),
                      dict(radius_meters=800),dict(min_injured=-1)]:
            with self.assertRaises(psycopg.errors.InvalidParameterValue):
                with self.conn.transaction():
                    count(**extra)

    def test_malformed_csv_and_invalid_batch_size(self):
        with self.assertRaises(ValueError):
            load_collisions(self.conn,self.path,batch_size=0)
        self.path.write_text('REPORT_ID\n1\n',encoding='utf-8')
        with self.assertRaises(ValueError):
            load_collisions(self.conn,self.path)


if __name__ == '__main__':
    unittest.main()
