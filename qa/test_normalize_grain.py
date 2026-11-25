"""normalize.grain_collapse: join-grain source layers (one row per parcel x sub-record) collapse
to one row per (declared key..., geometry), keep-first; counties without a declared key or with
the key column missing are left untouched. Offline, synthetic, no canonical data."""
import os
import sys
import unittest

import geopandas as gpd
from shapely.geometry import box

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
import normalize  # noqa: E402


def _join_layer():
    # OBJECTID = parcel polygon key (non-unique); OBJECTID_12 = join-row id (unique);
    # REPROPKEY = improvement record. Parcel 1: 3 improvement rows on ONE polygon (collapse to 1).
    # Parcel 2: 2 rows with DIFFERENT geometries (multi-part parcel: keep both).
    # Parcel 3: single row.
    rows = [
        dict(OBJECTID_12=1, OBJECTID=1, REPROPKEY="a", YR=1990, geometry=box(0, 0, 1, 1)),
        dict(OBJECTID_12=2, OBJECTID=1, REPROPKEY="b", YR=2001, geometry=box(0, 0, 1, 1)),
        dict(OBJECTID_12=3, OBJECTID=1, REPROPKEY="c", YR=2010, geometry=box(0, 0, 1, 1)),
        dict(OBJECTID_12=4, OBJECTID=2, REPROPKEY="d", YR=1975, geometry=box(2, 0, 3, 1)),
        dict(OBJECTID_12=5, OBJECTID=2, REPROPKEY="e", YR=1975, geometry=box(4, 0, 5, 1)),
        dict(OBJECTID_12=6, OBJECTID=3, REPROPKEY="f", YR=1960, geometry=box(6, 0, 7, 1)),
    ]
    return gpd.GeoDataFrame(rows, geometry="geometry", crs=4326)


class GrainCollapseTest(unittest.TestCase):
    def setUp(self):
        self._saved = dict(normalize.GRAIN_KEYS)
        normalize.GRAIN_DROPS.clear()

    def tearDown(self):
        normalize.GRAIN_KEYS.clear()
        normalize.GRAIN_KEYS.update(self._saved)
        normalize.GRAIN_DROPS.clear()

    def test_collapses_to_one_row_per_key_and_geometry_keep_first(self):
        normalize.GRAIN_KEYS["99999"] = ["OBJECTID"]
        out, dropped = normalize.grain_collapse(_join_layer(), "99999")
        self.assertEqual(dropped, 2)
        self.assertEqual(len(out), 4)
        # keep-first: parcel 1 retains its first improvement row
        p1 = out[out.OBJECTID == 1]
        self.assertEqual(len(p1), 1)
        self.assertEqual(p1.iloc[0].REPROPKEY, "a")
        # distinct geometries under one key are both retained (multi-part parcel)
        self.assertEqual(int((out.OBJECTID == 2).sum()), 2)
        self.assertEqual(out.crs.to_epsg(), 4326)

    def test_no_declared_key_is_a_noop(self):
        gdf = _join_layer()
        out, dropped = normalize.grain_collapse(gdf, "99998")
        self.assertEqual(dropped, 0)
        self.assertEqual(len(out), len(gdf))

    def test_missing_key_column_skips_collapse_rather_than_guessing(self):
        normalize.GRAIN_KEYS["99997"] = ["PARCEL_OID_NOT_PRESENT"]
        gdf = _join_layer()
        out, dropped = normalize.grain_collapse(gdf, "99997")
        self.assertEqual(dropped, 0)
        self.assertEqual(len(out), len(gdf))

    def test_exact_duplicate_rows_without_key_columns_differ_are_dropped_too(self):
        # rows identical on key AND geometry are collapsed even when every other attr matches
        normalize.GRAIN_KEYS["99996"] = ["OBJECTID"]
        gdf = _join_layer()
        gdf = gpd.GeoDataFrame(gdf.drop(columns=["OBJECTID_12", "REPROPKEY"]), geometry="geometry", crs=4326)
        out, dropped = normalize.grain_collapse(gdf, "99996")
        self.assertEqual((len(out), dropped), (4, 2))


if __name__ == "__main__":
    unittest.main()
