"""Offline tests for the 2026-09-28 mis-map fixes: normalize's lot-area guard + value-name
rejections, and recon/fix_mismaps_20260928.py's in-place repair (synthetic files only)."""
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

import geopandas as gpd
import pandas as pd
import pyarrow.parquet as pq
from shapely.geometry import Point

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import normalize  # noqa: E402

spec = importlib.util.spec_from_file_location("fix_mismaps", ROOT / "recon" / "fix_mismaps_20260928.py")
fix = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fix)


class LotAreaGuard(unittest.TestCase):
    def test_lot_size_column_is_flagged(self):
        acres = pd.Series([0.25, 0.5, 1.0, 2.0] * 50)
        lot = acres * 43560 * 1.03                      # roll lot size ~ GIS acreage
        self.assertGreaterEqual(normalize._lot_area_share(lot, acres), normalize.LOT_AREA_MAX_SHARE)

    def test_living_area_is_not_flagged(self):
        acres = pd.Series([0.25, 0.5, 1.0, 2.0] * 50)
        living = pd.Series([1400, 2100, 1750, 2600] * 50)
        self.assertLess(normalize._lot_area_share(living, acres), normalize.LOT_AREA_MAX_SHARE)

    def test_too_few_rows_is_undecided(self):
        self.assertIsNone(normalize._lot_area_share(pd.Series([10890.0]), pd.Series([0.25])))

    def test_sqft_living_pattern_and_polygon_area_decoys(self):
        cols = ["SQ_FT", "SQFT_LIVING", "GEOM_SQFT"]
        decoy = tuple(c for c in cols if normalize.re.search(normalize.SQFT_DECOY, c, normalize.re.I))
        self.assertEqual(decoy, ("GEOM_SQFT",))
        self.assertEqual(normalize._pick(cols, normalize.SQFT_PATTERNS, exclude=decoy), "SQFT_LIVING")


class ValueNameRejections(unittest.TestCase):
    def test_area_and_sale_price_are_not_auto_values(self):
        for name in ("TOTAL_LIVING_AREA", "LastSalePrice", "SALE_PRICE", "purchase_price"):
            self.assertTrue(normalize._bad_value(name), name)
        for name in ("TOTAL_VALUE", "SUB_TOTAL_VALUE", "TOTAL_TAXABLE_VALUE"):
            self.assertFalse(normalize._bad_value(name), name)

    def test_auto_detect_no_longer_picks_living_area(self):
        # Haakon SD: auto-detect used to pick TOTAL_LIVING_AREA (^total, median 1,340 'dollars')
        gdf = pd.DataFrame({"TOTAL_LIVING_AREA": [1340, 1500, 2100], "SUB_TOTAL_VALUE": [104685, 175192, 279862]})
        self.assertNotEqual(normalize._value_plan(gdf, list(gdf.columns), None)[1], "TOTAL_LIVING_AREA")
        # ...and the crosswalk's declared value_field is honored
        self.assertEqual(normalize._value_plan(gdf, list(gdf.columns), "SUB_TOTAL_VALUE"), ("col", "SUB_TOTAL_VALUE"))

    def test_declared_none_means_no_value(self):
        gdf = pd.DataFrame({"Sale_Price": [15, 15, 15]})
        self.assertEqual(normalize._value_plan(gdf, list(gdf.columns), "NONE"), (None, None))

    def test_apn_overrides_for_queens_and_st_louis_county(self):
        self.assertEqual(normalize.APN_OVERRIDES["36081"], "SBL")
        self.assertEqual(normalize.APN_OVERRIDES["29189"], "LOCATOR")


class InPlaceRepair(unittest.TestCase):
    def _write(self, tmp, df):
        path = os.path.join(tmp, "99001_test.parquet")
        gpd.GeoDataFrame(df, geometry=[Point(0, i) for i in range(len(df))], crs=4326).to_parquet(path)
        return path

    def _manifest(self, tmp, fmap):
        mpath = os.path.join(tmp, "manifest.json")
        with open(mpath, "w", encoding="utf-8") as f:
            json.dump({"99001": {"field_map": fmap, "populated": {"assessed_value": 1.0}}}, f)
        return mpath

    def test_lot_area_sqft_is_nulled_and_geo_metadata_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            acres = [0.25, 0.5, 1.0, 2.0] * 20
            df = pd.DataFrame({"acreage": acres,
                               "building_sqft": pd.array([round(a * 43560) for a in acres], dtype="Int64"),
                               "assessed_value": pd.array([100000] * 80, dtype="Int64")})
            path = self._write(tmp, df)
            mpath = self._manifest(tmp, {"building_sqft": "SQ_FT", "assessed_value": "TOTAL"})
            entry = {"fips": "99001", "field_map": {"building_sqft": "SQ_FT"}}
            changes, table = fix.plan_file(path, entry, "sqft")
            self.assertEqual(changes["rows_nulled"], 80)
            fix.apply_change("99001", path, changes, table, manifest_path=mpath)
            t = pq.read_table(path)
            self.assertEqual(t.column("building_sqft").null_count, 80)
            self.assertEqual(t.column("assessed_value").null_count, 0)
            self.assertIn(b"geo", t.schema.metadata)
            with open(mpath, encoding="utf-8") as f:
                m = json.load(f)["99001"]
            self.assertIsNone(m["field_map"]["building_sqft"])
            self.assertEqual(m["mismap_fix"]["building_sqft"]["was"], "SQ_FT")

    def test_real_building_sqft_is_left_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            df = pd.DataFrame({"acreage": [0.25, 0.5, 1.0, 2.0] * 20,
                               "building_sqft": pd.array([1400, 2100, 1750, 2600] * 20, dtype="Int64")})
            path = self._write(tmp, df)
            changes, table = fix.plan_file(path, {"field_map": {"building_sqft": "SQ_FT"}}, "sqft")
            self.assertIsNone(table)
            self.assertIn("skip", changes)

    def test_sale_price_move_fills_empty_sale_price_then_nulls_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            df = pd.DataFrame({"assessed_value": pd.array([150000, None, 90000], dtype="Int64"),
                               "sale_price": pd.array([None, None, 80000], dtype="Int64")})
            path = self._write(tmp, df)
            entry = {"fips": "99001", "field_map": {"assessed_value": "SALE_PRICE"}}
            changes, table = fix.plan_file(path, entry, "sale", "move")
            self.assertEqual(changes["sale_price_filled"], 1)       # row 3 keeps its own sale price
            self.assertEqual(table.column("assessed_value").null_count, 3)
            self.assertEqual(table.column("sale_price").to_pylist(), [150000, None, 80000])

    def test_laurens_is_null_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            df = pd.DataFrame({"assessed_value": pd.array([15, 15], dtype="Int64"),
                               "sale_price": pd.array([None, None], dtype="Int64")})
            path = self._write(tmp, df)
            entry = {"fips": "45059", "field_map": {"assessed_value": "Sale_Price"}}
            changes, table = fix.plan_file(path, entry, "sale", "move")
            self.assertNotIn("sale_price_filled", changes)
            self.assertEqual(table.column("sale_price").null_count, 2)

    def test_area_value_requires_area_source_and_small_median(self):
        with tempfile.TemporaryDirectory() as tmp:
            df = pd.DataFrame({"assessed_value": pd.array([150000, 220000], dtype="Int64")})
            path = self._write(tmp, df)
            changes, table = fix.plan_file(path, {"field_map": {"assessed_value": "TOTAL_LIVING_AREA"}}, "area")
            self.assertIsNone(table)                                 # dollar-sized -> refuse


if __name__ == "__main__":
    unittest.main()
