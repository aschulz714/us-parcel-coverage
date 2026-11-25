"""Offline staging safety tests; all files live in an isolated temporary root."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import geopandas as gpd
import pyarrow as pa
import pyarrow.parquet as pq
import shapely
from shapely.geometry import box

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import stage_tract_repair as repair


class StagingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="tract-repair-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.tracts = gpd.GeoDataFrame({"GEOID": ["53001000100", "53001000200"]},
                                      geometry=[box(0, 0, 2, 2), box(10, 10, 12, 12)], crs=4326)
        geo = {"primary_column": "geometry", "columns": {
            "geometry": {"encoding": "WKB", "crs": "EPSG:4326"}}}
        schema = pa.schema([
            pa.field("apn", pa.string(), metadata={b"source": b"preserve"}),
            pa.field("geometry", pa.binary()),
            pa.field("census_tract", pa.string(), metadata={b"vintage": b"2023"}),
            pa.field("opportunity_zone", pa.bool_()),
            pa.field("owner_occupied", pa.bool_()),
        ], metadata={b"geo": json.dumps(geo).encode(), b"custom": b"unchanged"})
        self.original = pa.Table.from_pydict({
            "apn": ["0001", "0002", "0003", "0004"],
            "geometry": [None, shapely.to_wkb(box(.2, .2, .8, .8)),
                         shapely.to_wkb(box(10.2, 10.2, 10.8, 10.8)), shapely.to_wkb(shapely.Polygon())],
            "census_tract": [None, "53001000200", None, None],
            "opportunity_zone": [None, True, False, None],
            "owner_occupied": [True, False, None, True],
        }, schema=schema)
        self.sources = self.root / "data/parcels/normalized"
        self.sources.mkdir(parents=True)
        self.source = self.sources / "53001_fixture.parquet"
        pq.write_table(self.original, self.source)
        self.reference = self.root / "data/reference/tiger/tl_2023_53_tract.zip"
        self.reference.parent.mkdir(parents=True)
        self.reference.write_bytes(b"synthetic cached reference; loader mocked")
        self.entries = {"53001": {"normalized_file": "data/parcels/normalized/53001_fixture.parquet"}}
        self.batch = self.root / ".tmp/tract-repair/pilot"
        self.loader = patch.object(repair, "load_cached_tracts", return_value=self.tracts)
        self.mock_loader = self.loader.start()
        self.addCleanup(self.loader.stop)

    def run_batch(self, **kwargs):
        return repair.run_batch(self.root, self.entries, **kwargs)

    def report(self):
        return json.loads((self.batch / "53001/complete.json").read_text())

    def test_repair_preserves_fields_and_quarantines_oz(self):
        rebuilt = repair.repair_table(self.original, self.tracts)
        stats = repair.validate_rewrite(self.original, rebuilt, self.tracts)
        self.assertEqual(rebuilt["census_tract"].to_pylist(), [None, "53001000100", "53001000200", None])
        self.assertEqual(rebuilt["opportunity_zone"].null_count, 4)
        self.assertTrue(rebuilt["owner_occupied"].equals(self.original["owner_occupied"]))
        self.assertTrue(rebuilt.schema.equals(self.original.schema, check_metadata=True))
        self.assertEqual(stats["assigned_spatially_verified"], 2)

    def test_validator_rejects_wrong_and_missing_matches(self):
        rebuilt = repair.repair_table(self.original, self.tracts)
        for ids in ([None, "53001000200", "53001000100", None], [None] * 4):
            with self.subTest(ids=ids), self.assertRaises(ValueError):
                bad = rebuilt.set_column(2, rebuilt.schema.field(2), pa.array(ids, type=pa.string()))
                repair.validate_rewrite(self.original, bad, self.tracts)

    def test_validator_rejects_unrelated_changes(self):
        rebuilt = repair.repair_table(self.original, self.tracts)
        bad = rebuilt.set_column(4, rebuilt.schema.field(4), pa.array([None] * 4, type=pa.bool_()))
        with self.assertRaisesRegex(ValueError, "Unrelated column"):
            repair.validate_rewrite(self.original, bad, self.tracts)

    def test_unknown_crs_fails_closed(self):
        metadata = dict(self.original.schema.metadata)
        geo = json.loads(metadata[b"geo"])
        geo["columns"]["geometry"]["crs"] = None
        metadata[b"geo"] = json.dumps(geo).encode()
        with self.assertRaisesRegex(ValueError, "CRS"):
            repair.repair_table(self.original.replace_schema_metadata(metadata), self.tracts)

    def test_invalid_reference_ids_rejected(self):
        with self.assertRaisesRegex(ValueError, "Wrong-state"):
            repair.check_tracts(self.tracts, "06")
        duplicate = self.tracts.copy()
        duplicate["GEOID"] = ["53001000100"] * 2
        with self.assertRaisesRegex(ValueError, "duplicate"):
            repair.repair_table(self.original, duplicate)

    def test_success_and_resume_verify_artifacts_without_rewriting(self):
        original_hash = repair.digest(self.source)
        result = self.run_batch()
        self.assertEqual(result["status"], "validated_staging_only")
        report = self.report()
        self.assertEqual(report["changed_tract_rows"], 2)
        self.assertEqual(report["oz_values_quarantined"], 2)
        artifact = self.batch / "53001" / report["staged"]
        written = artifact.stat().st_mtime_ns
        with patch.object(repair, "stage_county", side_effect=AssertionError("must resume")):
            resumed = self.run_batch()
        self.assertEqual(resumed["resumed"], 1)
        self.assertEqual(artifact.stat().st_mtime_ns, written)
        self.assertEqual(repair.digest(self.source), original_hash)
        self.assertEqual(repair.digest(self.batch / "53001" / report["backup"]), original_hash)

    def test_tampered_staging_is_rebuilt_in_new_attempt(self):
        self.run_batch()
        first = self.report()
        old_artifact = self.batch / "53001" / first["staged"]
        old_artifact.write_bytes(b"corrupted")
        result = self.run_batch()
        self.assertEqual(result["resumed"], 0)
        self.assertNotEqual(first["staged"], self.report()["staged"])
        self.assertTrue(old_artifact.exists())

    def test_source_and_reference_changes_invalidate_resume(self):
        self.run_batch()
        first = self.report()
        # Preserve table/schema, change physical encoding so the content hash changes.
        pq.write_table(self.original, self.source, compression="NONE")
        self.reference.write_bytes(b"new reference version")
        result = self.run_batch()
        self.assertEqual(result["resumed"], 0)
        self.assertNotEqual(first["staged"], self.report()["staged"])

    def test_missing_reference_records_failure_without_download(self):
        self.reference.unlink()
        result = self.run_batch()
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["status"], "failed")
        self.mock_loader.assert_not_called()
        self.assertFalse((self.batch / "53001/complete.json").exists())

    def test_failed_validation_never_marks_complete_and_can_retry(self):
        original_hash = repair.digest(self.source)
        with patch.object(repair, "validate_rewrite", side_effect=ValueError("injected validation failure")):
            result = self.run_batch()
        self.assertEqual(result["failed"], 1)
        self.assertFalse((self.batch / "53001/complete.json").exists())
        self.assertEqual(repair.digest(self.source), original_hash)
        self.assertEqual(self.run_batch()["validated"], 1)

    def test_write_failure_never_marks_complete(self):
        with patch.object(repair.pq, "write_table", side_effect=OSError("injected disk full")):
            result = self.run_batch()
        self.assertEqual(result["failed"], 1)
        self.assertFalse((self.batch / "53001/complete.json").exists())

    def test_source_mutation_during_validation_blocks_completion(self):
        real_validator = repair.validate_rewrite

        def mutate_source(*args):
            stats = real_validator(*args)
            pq.write_table(self.original, self.source, compression="NONE")
            return stats

        with patch.object(repair, "validate_rewrite", side_effect=mutate_source):
            result = self.run_batch()
        self.assertEqual(result["failed"], 1)
        self.assertIn("source changed", result["counties"]["53001"]["error"])
        self.assertFalse((self.batch / "53001/complete.json").exists())

    def test_corrupt_checkpoint_is_not_trusted(self):
        self.run_batch()
        first = self.report()
        changed = dict(first, rows=999)
        (self.batch / "53001/complete.json").write_text(json.dumps(changed), encoding="utf-8")
        result = self.run_batch()
        self.assertEqual(result["resumed"], 0)
        self.assertEqual(self.report()["rows"], 4)

    def test_malformed_wkb_fails_without_changing_source(self):
        broken = self.original.set_column(1, self.original.schema.field(1),
                                           pa.array([b"not WKB"] * 4, type=pa.binary()))
        pq.write_table(broken, self.source)
        original_hash = repair.digest(self.source)
        result = self.run_batch()
        self.assertEqual(result["failed"], 1)
        self.assertEqual(repair.digest(self.source), original_hash)
        self.assertFalse((self.batch / "53001/complete.json").exists())

    def test_bounded_run_reports_pending_and_resume_continues(self):
        other = self.sources / "53003_fixture.parquet"
        pq.write_table(self.original, other)
        self.entries["53003"] = {"normalized_file": "data/parcels/normalized/53003_fixture.parquet"}
        result = self.run_batch(max_counties=1)
        self.assertEqual((result["status"], result["validated"], result["pending"]), ("partial", 1, 1))
        result = self.run_batch(max_counties=1)
        self.assertEqual((result["validated"], result["resumed"], result["pending"]), (2, 1, 0))

    def test_failed_county_does_not_hide_successful_county(self):
        self.entries["53003"] = {"normalized_file": "data/parcels/normalized/53003_missing.parquet"}
        result = self.run_batch()
        self.assertEqual((result["validated"], result["failed"], result["pending"]), (1, 1, 0))
        self.assertEqual(result["status"], "failed")

    def test_path_escape_and_invalid_batch_rejected(self):
        with self.assertRaisesRegex(ValueError, "batch"):
            self.run_batch(batch="../../data")
        self.entries["53001"]["normalized_file"] = "requirements.txt"
        self.assertEqual(self.run_batch()["failed"], 1)

    def test_lock_released_after_interruption(self):
        self.batch.mkdir(parents=True)
        with self.assertRaises(KeyboardInterrupt):
            with repair.batch_lock(self.batch):
                raise KeyboardInterrupt
        with repair.batch_lock(self.batch):
            pass

    def test_second_writer_cannot_acquire_batch_lock(self):
        self.batch.mkdir(parents=True)
        with repair.batch_lock(self.batch):
            with self.assertRaises(OSError):
                with repair.batch_lock(self.batch):
                    self.fail("Concurrent lock unexpectedly acquired")


if __name__ == "__main__":
    unittest.main(verbosity=2)
