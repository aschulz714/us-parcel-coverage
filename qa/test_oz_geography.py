"""Original-round OZ geography regressions, with no network or real parcel writes."""
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import geopandas as gpd
import pandas as pd
import pyarrow as pa
import shapely
from shapely.geometry import box

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import geo_enrich
import oz_geography as oz
import stage_tract_repair as repair
import test_stage_tract_repair as fixtures


class OpportunityZoneTests(unittest.TestCase):
    def setUp(self):
        fixtures.StagingTests.setUp(self)
        # One original zone spans two modern tracts, neither reuses its GEOID.
        self.zones = gpd.GeoDataFrame({"GEOID": ["53001999900"]},
                                     geometry=[box(0, 0, 1, 2)], crs=4326)

    def flags(self, geoms, coverage=None, zones=None):
        table = pa.table({"geometry": pa.array(
            [None if geom is None else shapely.to_wkb(geom) for geom in geoms], type=pa.binary())})
        if coverage is None:
            coverage = [True] * len(geoms)
        reference = self.zones if zones is None else zones
        actual = oz.classify(table, reference, coverage)
        oz.validate_flags(table, reference, coverage, actual)
        return actual.astype(object).where(actual.notna(), None).tolist()

    def test_split_modern_ids_do_not_hide_original_zone(self):
        modern = gpd.GeoDataFrame({"GEOID": ["53001012301", "53001012302"]},
                                 geometry=[box(0, 0, .5, 2), box(.5, 0, 1, 2)], crs=4326)
        table = pa.table({"geometry": [shapely.to_wkb(shapely.Point(.25, 1)),
                                       shapely.to_wkb(shapely.Point(.75, 1))]})
        result = geo_enrich.enrich_table(table, modern, {"53001999900"}, self.zones)
        self.assertEqual(result["census_tract"].tolist(), ["53001012301", "53001012302"])
        self.assertEqual(result["opportunity_zone_2018"].tolist(), [True, True])

    def test_merged_or_reused_id_does_not_expand_old_zone(self):
        modern = self.tracts.copy()
        modern.loc[0, "GEOID"] = "53001999900"
        table = pa.table({"geometry": [shapely.to_wkb(shapely.Point(1.5, 1))]})
        result = geo_enrich.enrich_table(table, modern, {"53001999900"}, self.zones)
        self.assertEqual(result["census_tract"].tolist(), ["53001999900"])
        self.assertEqual(result["opportunity_zone"].tolist(), [False])

    def test_missing_boundaries_never_fall_back_to_id_membership(self):
        result = geo_enrich.enrich_table(self.original, self.tracts, {"53001000100"})
        self.assertTrue(result["opportunity_zone"].isna().all())
        self.assertTrue(result["opportunity_zone_2018"].isna().all())

    def test_legacy_tract_column_alone_does_not_skip_oz_repair(self):
        entries = [dict(self.entries["53001"], fips="53001", county="Fixture", state="WA")]
        with patch.object(geo_enrich, "ROOT", str(self.root)), patch.object(
                oz, "load_zones", return_value=self.zones), patch.object(
                geo_enrich, "load_tracts", return_value=self.tracts), patch.object(
                geo_enrich, "write_columns") as writer, patch.object(
                geo_enrich, "has_col", side_effect=lambda path, col: col == "census_tract"):
            geo_enrich.process(entries, skip_existing=True)
            writer.assert_called_once()
        with patch.object(oz, "load_zones", return_value=self.zones), patch.object(
                geo_enrich, "write_columns") as writer, patch.object(geo_enrich, "has_col", return_value=True):
            geo_enrich.process(entries, skip_existing=True)
            writer.assert_not_called()

    def test_invalid_missing_empty_boundary_and_outside(self):
        invalid = shapely.Polygon([(0, 0), (1, 1), (1, 0), (0, 1), (0, 0)])
        geoms = [None, invalid, shapely.Polygon(), shapely.Point(0, 1),
                 shapely.Point(.5, 1), shapely.Point(1.5, 1)]
        self.assertEqual(self.flags(geoms), [None, None, None, None, True, False])

    def test_unknown_coverage_cannot_create_false_negative(self):
        self.assertEqual(self.flags([shapely.Point(.5, 1), shapely.Point(3, 3)], [False, False]),
                         [True, None])

    def test_quarantined_reference_envelope_is_unknown_not_zone(self):
        zones = self.zones.copy()
        zones["quarantined"] = True
        self.assertEqual(self.flags([shapely.Point(.5, 1), shapely.Point(1.5, 1)], zones=zones),
                         [None, False])

    def test_multiple_zone_matches_remain_unknown(self):
        zones = gpd.GeoDataFrame({"GEOID": ["53001999900", "53001999901"]},
                                 geometry=[box(0, 0, 1, 2), box(.2, .2, .8, 1.8)], crs=4326)
        self.assertEqual(self.flags([shapely.Point(.5, 1)], zones=zones), [None])

    def test_incorrect_flags_fail_independent_validation(self):
        with self.assertRaises(AssertionError):
            oz.validate_flags(self.original, self.zones, [False, True, True, False],
                              pd.Series([None, False, False, None], dtype="boolean"))

    def test_restore_adds_provenance_and_preserves_other_fields(self):
        rebuilt = repair.repair_table(self.original, self.tracts, self.zones)
        stats = repair.validate_rewrite(self.original, rebuilt, self.tracts, self.zones)
        self.assertEqual((stats["oz_true"], stats["oz_false"], stats["oz_unknown"]), (1, 1, 2))
        self.assertEqual(rebuilt.schema.field("opportunity_zone_2018"), oz.versioned_field())
        self.assertEqual(rebuilt.schema.metadata, self.original.schema.metadata)

    def test_restored_batch_resume_and_mode_change(self):
        archive = oz.cache_path(self.root)
        archive.write_bytes(b"mocked official cache")
        with patch.object(oz, "load_zones", return_value=self.zones):
            result = repair.run_batch(self.root, self.entries, restore_oz=True)
            self.assertEqual(result["validated"], 1)
            result = repair.run_batch(self.root, self.entries, restore_oz=True)
            self.assertEqual(result["resumed"], 1)
        # Switching back to quarantine may not resume the restored flags.
        result = repair.run_batch(self.root, self.entries)
        self.assertEqual(result["resumed"], 0)

    def test_missing_or_untrusted_archive_fails_closed(self):
        result = repair.run_batch(self.root, self.entries, restore_oz=True)
        self.assertEqual(result["failed"], 1)
        oz.cache_path(self.root).write_bytes(b"not the official zip")
        with self.assertRaisesRegex(ValueError, "checksum"):
            oz.load_zones(self.root)

    def test_reference_change_invalidates_restored_resume(self):
        archive = oz.cache_path(self.root)
        archive.write_bytes(b"mock official reference")
        with patch.object(oz, "load_zones", return_value=self.zones):
            repair.run_batch(self.root, self.entries, restore_oz=True)
        archive.write_bytes(b"changed reference")
        result = repair.run_batch(self.root, self.entries, restore_oz=True)
        self.assertEqual((result["resumed"], result["failed"]), (0, 1))

    def test_reference_change_between_counties_rejects_cached_geometry(self):
        import pyarrow.parquet as pq
        archive = oz.cache_path(self.root)
        archive.write_bytes(b"mock official reference")
        pq.write_table(self.original, self.sources / "53003_fixture.parquet")
        self.entries["53003"] = {"normalized_file": "data/parcels/normalized/53003_fixture.parquet"}
        real_stage = repair.stage_county

        def mutate_after_first(*args):
            report = real_stage(*args)
            archive.write_bytes(b"changed after first county")
            return report

        with patch.object(oz, "load_zones", return_value=self.zones), patch.object(
                repair, "stage_county", side_effect=mutate_after_first):
            result = repair.run_batch(self.root, self.entries, restore_oz=True)
        self.assertEqual((result["validated"], result["failed"]), (1, 1))
        self.assertIn("cached geometry is stale", result["counties"]["53003"]["error"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
