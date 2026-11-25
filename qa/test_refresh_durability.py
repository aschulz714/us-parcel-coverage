"""Offline tests for the staged-generation refresh path (src/refresh.py, 2026-09-14).

No network, no canonical data: a temp PARCELS tree with a synthetic raw + canonical file,
ingest.ingest_county stubbed to return the raw, geo_enrich stubbed to add the geography
columns. Verifies fips injection, method dispatch, fail-closed validation, and that a
rejected generation never touches the canonical file or manifest.
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import geopandas as gpd
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from shapely.geometry import box

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))
os.chdir(SRC)
import refresh  # noqa: E402
import ingest  # noqa: E402
import raw_inventory  # noqa: E402
from parquet_cols import write_columns  # noqa: E402

N = 80


def make_raw(path, n=N, apn_prefix="A", owner_null_frac=0.0):
    gdf = gpd.GeoDataFrame({
        "OBJECTID": range(1, n + 1),
        "PARCEL_ID": [f"{apn_prefix}{i:05d}" for i in range(n)],
        "OWNER": [None if i < int(n * owner_null_frac) else f"OWNER {i}" for i in range(n)],
        "MAIL_ADDR": [f"{i} PINE ST" for i in range(n)],
        "TOTAL_VAL": [100000 + i for i in range(n)],
        "LAND_USE": ["R"] * n,
    }, geometry=[box(-118.4 + i * 0.001, 47.6, -118.4 + i * 0.001 + 0.0008, 47.6008) for i in range(n)],
        crs=4326)
    gdf.to_parquet(path)
    return path


def stub_geo(entry):
    """Stand-in for geo_enrich.process: add the geography columns without TIGER/OZ."""
    path = os.path.join(refresh.ROOT, entry["normalized_file"])
    table = pq.read_table(path)
    n = table.num_rows
    write_columns(path, table, {
        "census_tract": pd.Series(["53043950100"] * n, dtype="string"),
        "opportunity_zone": pd.Series([False] * n, dtype="boolean"),
        "opportunity_zone_2018": pd.Series([False] * n, dtype="boolean"),
        "owner_occupied": pd.Series([pd.NA] * n, dtype="boolean"),
    })


class RefreshDurabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="refresh-test-"))
        self.parcels = self.tmp / "data" / "parcels"
        (self.parcels / "raw").mkdir(parents=True)
        (self.parcels / "normalized").mkdir()
        self.manifest = self.parcels / "manifest.json"
        # point refresh + ingest at the temp tree (module globals, restored in tearDown)
        self._saved = {m: (m.PARCELS, m.MANIFEST) for m in (refresh,)}
        refresh.PARCELS = str(self.parcels)
        refresh.MANIFEST = str(self.manifest)
        refresh.GEN_DIR = str(self.parcels / "normalized_gen")
        # HARD ISOLATION (2026-09-14 incident): an earlier fixture used 53043, which is in
        # refresh.BULK_FIPS; pull_county routed to the real WA bulk builder, which called the
        # stubbed ingest_county and wrote the fake entry into the REAL manifest through ingest's
        # default OUT_DIR. Three guards so that cannot recur:
        self._saved_out_dir = ingest.OUT_DIR
        ingest.OUT_DIR = str(self.parcels)                  # default manifest path -> temp tree
        # 2026-09-25: pull_county records every fresh raw into raw_inventory.json; unredirected,
        # this fixture's 80-row raw sat in the REAL inventory as Whitman's evidence since 09-14
        self._saved_inventory = raw_inventory.INVENTORY
        raw_inventory.INVENTORY = str(self.parcels / "raw_inventory.json")
        self._saved_bulk = refresh.bulk

        class _NoBulk:                                       # any bulk call is a test failure
            def __getattr__(self, name):
                raise AssertionError(f"bulk builder path reached in offline test ({name})")
        refresh.bulk = _NoBulk()
        self._saved_geo = refresh.geo_enrich_fn
        self._saved_ingest = ingest.ingest_county
        refresh.geo_enrich_fn = stub_geo
        self.fips = "53075"   # Whitman WA: a plain arcgis county (NOT in refresh.BULK_FIPS)
        assert self.fips not in refresh.BULK_FIPS
        self.raw = self.parcels / "raw" / f"{self.fips}_whitman.parquet"
        make_raw(str(self.raw))
        self.entry = {"county": "Whitman", "state": "WA", "file": str(self.raw),
                      "normalized_file": str(self.parcels / "normalized" / self.raw.name),
                      "owner_fields": "OWNER", "value_field": "TOTAL_VAL", "feature_count": N,
                      "source_url": "https://example.test/FeatureServer/0/query",
                      "retrieved_at": "2026-09-14T00:00:00Z", "source_vintage": "", "method": "arcgis"}
        # build the initial canonical the same way the pipeline would (via the staged path)
        self.manifest.write_text(json.dumps({self.fips: self.entry}), encoding="utf-8")
        gen = refresh.build_generation(dict(self.entry, fips=self.fips))
        refresh.promote_generation(gen)
        self.canonical = Path(gen["canonical"])
        self.assertTrue(self.canonical.exists())
        self.canon_bytes = self.canonical.read_bytes()
        self.crosswalk = {self.fips: {"fips": self.fips, "county": "Whitman", "state": "WA",
                                      "method": "arcgis", "query_url": self.entry["source_url"]}}

    def tearDown(self):
        refresh.PARCELS, refresh.MANIFEST = self._saved[refresh]
        refresh.geo_enrich_fn = self._saved_geo
        refresh.bulk = self._saved_bulk
        ingest.ingest_county = self._saved_ingest
        ingest.OUT_DIR = self._saved_out_dir
        raw_inventory.INVENTORY = self._saved_inventory
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _stub_ingest(self, n=N, apn_prefix="A", owner_null_frac=0.0):
        def fake(row, raw_dir, refresh=False, count_waiver_reason=None):
            make_raw(str(self.raw), n=n, apn_prefix=apn_prefix, owner_null_frac=owner_null_frac)
            return dict(self.entry, feature_count=n, fetched_count=n, retrieved_at="2026-09-14T01:00:00Z")
        ingest.ingest_county = fake

    def _manifest(self):
        return json.loads(self.manifest.read_text(encoding="utf-8"))

    def test_real_manifest_and_inventory_are_never_touched(self):
        reals = [Path(refresh.ROOT) / "data" / "parcels" / name
                 for name in ("manifest.json", "raw_inventory.json")]
        before = [p.read_bytes() if p.exists() else None for p in reals]
        self._stub_ingest(apn_prefix="Z")
        refresh.pull_county(dict(self.entry, fips=self.fips), self.crosswalk)
        self.assertEqual([p.read_bytes() if p.exists() else None for p in reals], before)
        self.assertIn(self.raw.name, raw_inventory.load())  # recorded, but in the temp inventory

    def test_load_entries_injects_manifest_key_as_fips(self):
        m = {"41041": {"normalized_file": "x.parquet"}, "53027": {"normalized_file": "y.parquet", "fips": "53027"},
             "99999": {}}
        entries = refresh.load_entries(m)
        self.assertEqual(sorted(e["fips"] for e in entries), ["41041", "53027"])

    def test_initial_generation_has_every_derived_column(self):
        names = pq.read_schema(self.canonical).names
        for c in refresh.REQUIRED_DERIVED:
            self.assertIn(c, names)
        self.assertTrue((self.parcels / "normalized_gen").exists() is False or
                        not list((self.parcels / "normalized_gen").rglob("*.parquet")),
                        "staged payload must not linger after promotion")

    def test_successful_refresh_promotes_and_updates_manifest_once(self):
        self._stub_ingest(apn_prefix="B")
        refresh.pull_county(dict(self.entry, fips=self.fips), self.crosswalk)
        t = pq.read_table(self.canonical)
        self.assertTrue(t["apn"].to_pylist()[0].startswith("B"))
        for c in refresh.REQUIRED_DERIVED:
            self.assertIn(c, t.column_names)
        m = self._manifest()[self.fips]
        self.assertEqual(m["normalized_row_count"], N)
        self.assertIn("refresh_checks", m)
        self.assertIn("refreshed_at", m)

    def test_row_collapse_is_rejected_and_canonical_untouched(self):
        before = self._manifest()[self.fips]
        self._stub_ingest(n=N // 2)
        with self.assertRaises(refresh.RefreshRejected) as cm:
            refresh.pull_county(dict(self.entry, fips=self.fips), self.crosswalk)
        self.assertIn("row retention", str(cm.exception))
        self.assertEqual(self.canonical.read_bytes(), self.canon_bytes)
        after = self._manifest()[self.fips]
        # only the raw-provenance fields from the ingest step may change; nothing normalized-side did
        for key in ("normalized_file", "normalized_row_count", "field_map", "refreshed_at", "refresh_checks"):
            self.assertEqual(after.get(key), before.get(key), key)
        # the rejected generation is retained under normalized_gen/<fips>/ for inspection
        self.assertTrue(list(Path(refresh.GEN_DIR).rglob("*.parquet")))

    def test_owner_truncation_is_rejected(self):
        # the review's silent owner-table truncation class: rows intact, owner population collapses
        self._stub_ingest(owner_null_frac=0.5)
        with self.assertRaises(refresh.RefreshRejected) as cm:
            refresh.pull_county(dict(self.entry, fips=self.fips), self.crosswalk)
        self.assertIn("owner population fell", str(cm.exception))
        self.assertEqual(self.canonical.read_bytes(), self.canon_bytes)

    def test_duplicate_apn_growth_is_rejected(self):
        def fake(row, raw_dir, refresh=False, count_waiver_reason=None):
            make_raw(str(self.raw))
            g = gpd.read_parquet(str(self.raw))
            g["PARCEL_ID"] = "SAME"          # every APN identical -> dup rate ~100%
            g.to_parquet(str(self.raw))
            return dict(self.entry, fetched_count=N)
        ingest.ingest_county = fake
        with self.assertRaises(refresh.RefreshRejected) as cm:
            refresh.pull_county(dict(self.entry, fips=self.fips), self.crosswalk)
        self.assertIn("duplicate-APN", str(cm.exception))
        self.assertEqual(self.canonical.read_bytes(), self.canon_bytes)

    def test_unsupported_method_raises_without_ingest(self):
        called = []
        ingest.ingest_county = lambda *a, **k: called.append(1)
        with self.assertRaises(RuntimeError):
            refresh.pull_county(dict(self.entry, fips=self.fips),
                                {self.fips: dict(self.crosswalk[self.fips], method="web-app-only")})
        self.assertEqual(called, [])

    def test_wfs_and_join_methods_are_dispatched(self):
        for method in ("wfs", "arcgis-join"):
            self._stub_ingest(apn_prefix=method[:1].upper())
            refresh.pull_county(dict(self.entry, fips=self.fips),
                                {self.fips: dict(self.crosswalk[self.fips], method=method)})
            self.assertTrue(pq.read_table(self.canonical)["apn"].to_pylist()[0].startswith(method[:1].upper()))


class StubPreviousGenerationTests(unittest.TestCase):
    """validate_generation against a canonical file that is only a stub (Malheur OR: 1 row before
    its 20,570-parcel repoint) — the stub's 0% duplicate rate is no baseline."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="refresh-stub-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def gen(self, name, apns):
        n = len(apns)
        cols = {"apn": apns, "owner": ["OWNER"] * n, "assessed_value": [100] * n,
                "geometry": [b"\x00"] * n, **{c: ["x"] * n for c in refresh.REQUIRED_DERIVED}}
        path = str(self.tmp / name)
        pq.write_table(pa.table(cols), path)
        return path

    def test_stub_previous_is_not_a_baseline(self):
        staged = self.gen("new.parquet", [f"P{i}" for i in range(180)] + ["P0"] * 20)  # 10% pieces
        checks = refresh.validate_generation(staged, self.gen("old.parquet", ["700"]), 200)
        self.assertEqual((checks["degenerate_previous"], checks["dup_rate"]), (True, (None, 0.1)))

    def test_stub_previous_still_caps_the_new_duplicate_rate(self):
        staged = self.gen("new.parquet", [str(i % 10) for i in range(200)])  # the wrong-key case
        with self.assertRaisesRegex(refresh.RefreshRejected, "stub"):
            refresh.validate_generation(staged, self.gen("old.parquet", ["700"]), 200)

    def test_real_previous_keeps_the_growth_rule(self):
        staged = self.gen("new.parquet", [f"P{i}" for i in range(180)] + ["P0"] * 20)
        old = self.gen("old.parquet", [f"P{i}" for i in range(190)])
        with self.assertRaisesRegex(refresh.RefreshRejected, "duplicate-APN rate grew"):
            refresh.validate_generation(staged, old, 200)


if __name__ == "__main__":
    unittest.main(verbosity=1)
