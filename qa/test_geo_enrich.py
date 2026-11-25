"""Offline regression tests for parcel-to-tract row identity."""
import os
import sys
import unittest

import geopandas as gpd
import pandas as pd
import pyarrow as pa
import shapely
from shapely.geometry import Polygon, box

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "src"))
from geo_enrich import enrich_table


class TractAlignmentTests(unittest.TestCase):
    def setUp(self):
        self.tracts = gpd.GeoDataFrame(
            {"GEOID": ["53001000100", "53001000200"]},
            geometry=[box(0, 0, 2, 2), box(10, 10, 12, 12)], crs=4326)
        self.a = box(.2, .2, .8, .8)
        self.b = box(10.2, 10.2, 10.8, 10.8)

    def check_assignments(self, geometries, expected):
        table = pa.table({"geometry": pa.array(
            [None if g is None else shapely.to_wkb(g) for g in geometries],
            type=pa.binary())})
        result = enrich_table(table, self.tracts, oz_boundaries=self.tracts.iloc[[0]])
        expected_tract = pd.Series(expected, dtype="string")
        pd.testing.assert_series_equal(result["census_tract"], expected_tract)
        expected_oz = pd.Series(
            [None if v is None else v == "53001000100" for v in expected],
            dtype="boolean")
        pd.testing.assert_series_equal(result["opportunity_zone"], expected_oz)
        self.assertEqual(len(result["owner_occupied"]), len(geometries))
        self.assertTrue(result["owner_occupied"].isna().all())

    def test_missing_first_geometry_does_not_shift_remaining_parcels(self):
        self.check_assignments([None, self.a, self.b],
                               [None, "53001000100", "53001000200"])

    def test_missing_middle_geometry_preserves_later_parcel_identity(self):
        self.check_assignments([self.a, None, self.b, self.a],
                               ["53001000100", None, "53001000200", "53001000100"])

    def test_invalid_polygon_does_not_shift_other_parcels(self):
        crossed = Polygon([(0, 0), (1, 1), (1, 0), (0, 1), (0, 0)])
        self.assertFalse(crossed.is_valid)
        self.check_assignments([crossed, self.a, crossed, self.b],
                               [None, "53001000100", None, "53001000200"])

    def test_empty_outside_and_boundary_geometries_remain_unassigned(self):
        self.check_assignments(
            [Polygon(), box(20, 20, 21, 21), shapely.Point(0, 1), self.b],
            [None, None, None, "53001000200"])

    def test_all_missing_and_empty_input(self):
        self.check_assignments([None, None], [None, None])
        self.check_assignments([], [])

    def test_all_valid_parcels_keep_original_order(self):
        self.check_assignments([self.b, self.a, self.b],
                               ["53001000200", "53001000100", "53001000200"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
