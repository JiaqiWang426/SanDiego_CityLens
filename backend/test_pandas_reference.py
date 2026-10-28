import unittest

import pandas as pd
from pyproj import Geod

from pandas_reference import count_service_requests_pandas


class PandasReferenceTests(unittest.TestCase):
    def setUp(self):
        self.df = pd.DataFrame([
            ('start', '', '2026-01-01 00:00:00', '32.8', '-117.2'),
            ('child', 'start', '2026-01-01 12:00:00', '32.8', '-117.2'),
            ('end', '', '2026-01-02 00:00:00', '32.8', '-117.2'),
            ('far', '', '2026-01-01 12:00:00', '32.8', '-118.2'),
            ('missing', '', '2026-01-01 12:00:00', '', ''),
            ('repeat', '', '2026-01-01 12:00:00', '32.8', '-117.2'),
            ('repeat', '', '2026-01-01 12:00:00', '32.8', '-118.2'),
        ], columns=['service_request_id', 'service_request_parent_id',
                    'date_requested', 'lat', 'lng'])

    def count(self, **kwargs):
        params = dict(latitude=32.8, longitude=-117.2, radius_meters=800,
                      start_time='2026-01-01T00:00:00-08:00',
                      end_time='2026-01-02T00:00:00-08:00')
        params.update(kwargs)
        return count_service_requests_pandas(self.df, **params)

    def test_filters_and_last_duplicate_wins(self):
        original = self.df.copy(deep=True)
        self.assertEqual(self.count(), 2)
        self.assertEqual(self.count(exclude_linked_duplicates=True), 1)
        self.assertEqual(self.count(radius_meters=0), 2)
        pd.testing.assert_frame_equal(original, self.df)

    def test_invalid_inputs(self):
        for override in [dict(latitude=91), dict(radius_meters=-1),
                         dict(start_time='2026-01-01'),
                         dict(end_time='2026-01-01T00:00:00-08:00')]:
            with self.subTest(override=override), self.assertRaises(ValueError):
                self.count(**override)

    def test_distance_threshold(self):
        lng, lat, _ = Geod(ellps='WGS84').fwd(-117.2, 32.8, 45, 800)
        self.df = self.df.iloc[:1].copy()
        self.df.loc[:, 'lat'] = str(lat)
        self.df.loc[:, 'lng'] = str(lng)
        self.assertEqual(self.count(radius_meters=799.99), 0)
        self.assertEqual(self.count(radius_meters=800.01), 1)


if __name__ == '__main__':
    unittest.main()
