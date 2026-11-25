"""Offline tests for the nominal-sale rule (user decision 2026-10-02): sale_price <= $100 is NULL,
both in normalize and in recon/fix_nominal_sales_20261002.py's in-place repair."""
import importlib.util
import json
import os
import sys
import tempfile
import unittest

import geopandas as gpd
import pandas as pd
import pyarrow.parquet as pq
from shapely.geometry import box

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
import normalize  # noqa: E402
import value_overlay  # noqa: E402

spec = importlib.util.spec_from_file_location(
    "fix_nominal_sales", os.path.join(ROOT, "recon", "fix_nominal_sales_20261002.py"))
fix = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fix)


class NormalizeFloor(unittest.TestCase):
    def test_nominal_sale_prices_are_null_but_sale_date_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            value_overlay.OVERLAY = os.path.join(tmp, "value_overlay")
            os.makedirs(value_overlay.OVERLAY)
            gdf = gpd.GeoDataFrame(
                {"PARCEL_ID": ["P1", "P2", "P3", "P4"], "OWNER": ["A", "B", "C", "D"],
                 "SALE_PRICE": [1, 100, 101, 250000],
                 "SALE_DATE": ["2025-01-02", "2025-02-03", "2025-03-04", "2025-04-05"]},
                geometry=[box(-105 + i * 0.001, 43, -105 + i * 0.001 + 0.0008, 43.0008) for i in range(4)],
                crs="EPSG:4326")
            raw = os.path.join(tmp, "56999_synth.parquet")
            gdf.to_parquet(raw)
            entry = {"file": raw, "fips": "56999", "state": "WY", "county": "Synth", "owner_fields": "OWNER",
                     "value_field": None, "source_url": "synthetic://t", "retrieved_at": "2026-10-02",
                     "source_last_edit": None}
            normalize.normalize_county(entry, os.path.join(tmp, "out"))
            out = gpd.read_parquet(os.path.join(tmp, "out", "56999_synth.parquet")).set_index("apn")
            self.assertEqual([None if pd.isna(v) else int(v) for v in out["sale_price"]], [None, None, 101, 250000])
            self.assertEqual(out["sale_date"].notna().sum(), 4)


class InPlaceRepair(unittest.TestCase):
    def test_clears_nominal_keeps_market_and_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "99001_t.parquet")
            gpd.GeoDataFrame({"sale_price": pd.array([1, 100, 101, None, 300000], dtype="Int64")},
                             geometry=[box(0, i, 1, i + 1) for i in range(5)], crs=4326).to_parquet(path)
            mpath = os.path.join(tmp, "manifest.json")
            with open(mpath, "w", encoding="utf-8") as f:
                json.dump({"99001": {"populated": {"sale_price": 0.8}}}, f)
            n, rows, table = fix.plan_file(path)
            self.assertEqual((n, rows), (2, 5))
            fix.apply_change("99001", path, n, table, manifest_path=mpath)
            t = pq.read_table(path)
            self.assertEqual(t.column("sale_price").to_pylist(), [None, None, 101, None, 300000])
            self.assertIn(b"geo", t.schema.metadata)
            with open(mpath, encoding="utf-8") as f:
                e = json.load(f)["99001"]
            self.assertEqual(e["populated"]["sale_price"], 0.4)
            self.assertEqual(e["nominal_sale_fix"]["rows_cleared"], 2)

    def test_multi_chunk_file_with_nulls_counts_exactly(self):
        # regression: pyarrow 21 fill_null put garbage in null slots of multi-chunk int64 columns
        import pyarrow as pa
        vals = [None, 50, 250000, None, 1, None, 99999, None, 100, 101] * 300
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "99003_t.parquet")
            pq.write_table(pa.table({"sale_price": pa.array(vals, type=pa.int64())}), path, row_group_size=7)
            n, rows = fix.count_file(path)
            self.assertEqual((n, rows), (3 * 300, 3000))                 # 50, 1, 100 per block
            n, _, table = fix.plan_file(path)
            out = table.column("sale_price").to_pylist()
            self.assertEqual(sum(v is not None for v in out), 3 * 300)   # 250000, 99999, 101 kept
            self.assertEqual(sorted({v for v in out if v is not None}), [101, 99999, 250000])

    def test_file_without_nominal_prices_is_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "99002_t.parquet")
            gpd.GeoDataFrame({"sale_price": pd.array([150000, None], dtype="Int64")},
                             geometry=[box(0, 0, 1, 1), box(0, 1, 1, 2)], crs=4326).to_parquet(path)
            n, _, table = fix.plan_file(path)
            self.assertEqual(n, 0)
            self.assertIsNone(table)


if __name__ == "__main__":
    unittest.main()
