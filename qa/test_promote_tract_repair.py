"""Offline safety tests for src/promote_tract_repair.py (no real data, no network)."""
import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import promote_tract_repair as promo  # noqa: E402


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_table(path, tracts):
    table = pa.table({"apn": pa.array([f"A{i}" for i in range(len(tracts))]),
                      "census_tract": pa.array(tracts, type=pa.string())})
    pq.write_table(table, path)


class PromoteTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="promote-test-"))
        self.norm = self.root / "data/parcels/normalized"
        self.norm.mkdir(parents=True)
        self.batch = "unit"
        self.county = self.root / ".tmp/tract-repair" / self.batch / "99001"
        self.attempt = self.county / "attempt-abc"
        self.attempt.mkdir(parents=True)
        self.canonical = self.norm / "99001_test.parquet"
        write_table(self.canonical, ["1", "2", None])
        shutil.copy2(self.canonical, self.attempt / "original.parquet")
        write_table(self.attempt / "repaired.parquet", ["1", "3", None])
        self.report = {
            "status": "validated_staging_only",
            "identity": {"source": str(self.canonical.resolve()), "source_sha256": sha(self.canonical),
                         "policy": "tract-and-official-2018-oz-v1", "reference_sha256": "r" * 64},
            "rows": 3, "changed_tract_rows": 1, "changed_oz_rows": 0,
            "backup": "attempt-abc/original.parquet", "backup_sha256": sha(self.canonical),
            "staged": "attempt-abc/repaired.parquet", "staged_sha256": sha(self.attempt / "repaired.parquet"),
        }
        self._save_report()

    def _save_report(self):
        (self.county / "complete.json").write_text(json.dumps(self.report), encoding="utf-8")
        (self.attempt / "validation.json").write_text(json.dumps(self.report), encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _ledger_lines(self):
        ledger = self.root / promo.LEDGER_REL
        return ledger.read_text(encoding="utf-8").splitlines() if ledger.exists() else []

    def test_dry_run_writes_nothing(self):
        before = sha(self.canonical)
        s = promo.promote_batch(self.root, self.batch, dry_run=True, log=lambda *_: None)
        self.assertEqual(s["dry_run_ok"], 1)
        self.assertEqual(sha(self.canonical), before)
        self.assertEqual(self._ledger_lines(), [])
        self.assertFalse((self.county / "promoted.json").exists())

    def test_promote_then_idempotent(self):
        s = promo.promote_batch(self.root, self.batch, log=lambda *_: None)
        self.assertEqual(s["promoted"], 1, s)
        self.assertEqual(sha(self.canonical), self.report["staged_sha256"])
        self.assertEqual(pq.read_table(self.canonical)["census_tract"].to_pylist(), ["1", "3", None])
        self.assertEqual(len(self._ledger_lines()), 1)
        rec = json.loads(self._ledger_lines()[0])
        self.assertEqual(rec["before_sha256"], self.report["identity"]["source_sha256"])
        self.assertEqual(rec["after_sha256"], self.report["staged_sha256"])
        self.assertTrue((self.attempt / "original.parquet").exists(), "no reclaim without flag")
        s2 = promo.promote_batch(self.root, self.batch, log=lambda *_: None)
        self.assertEqual(s2["already_promoted"], 1, s2)
        self.assertEqual(len(self._ledger_lines()), 1, "idempotent re-run must not re-append")

    def test_refuses_when_canonical_changed_after_staging(self):
        write_table(self.canonical, ["9", "9", "9"])
        s = promo.promote_batch(self.root, self.batch, log=lambda *_: None)
        self.assertEqual(s["refused"], 1)
        self.assertIn("changed since staging", s["counties"]["99001"]["error"])
        self.assertEqual(pq.read_table(self.canonical)["census_tract"].to_pylist(), ["9", "9", "9"])
        self.assertEqual(self._ledger_lines(), [])

    def test_refuses_when_staged_hash_mismatches(self):
        write_table(self.attempt / "repaired.parquet", ["1", "4", None])
        before = sha(self.canonical)
        s = promo.promote_batch(self.root, self.batch, log=lambda *_: None)
        self.assertEqual(s["refused"], 1)
        self.assertIn("Staged artifact hash mismatch", s["counties"]["99001"]["error"])
        self.assertEqual(sha(self.canonical), before)

    def test_refuses_unvalidated_status(self):
        self.report["status"] = "in_progress"
        self._save_report()
        s = promo.promote_batch(self.root, self.batch, log=lambda *_: None)
        self.assertEqual(s["refused"], 1)

    def test_refuses_row_count_mismatch(self):
        self.report["rows"] = 4
        self._save_report()
        s = promo.promote_batch(self.root, self.batch, log=lambda *_: None)
        self.assertEqual(s["refused"], 1)
        self.assertIn("rows", s["counties"]["99001"]["error"])

    def test_recovers_marker_after_crash_window(self):
        # Simulate: replace happened, marker/ledger did not.
        shutil.copyfile(self.attempt / "repaired.parquet", self.canonical)
        s = promo.promote_batch(self.root, self.batch, log=lambda *_: None)
        self.assertEqual(s["recovered"], 1, s)
        self.assertEqual(len(self._ledger_lines()), 1)
        self.assertTrue(json.loads(self._ledger_lines()[0])["recovered_marker"])

    def test_reclaim_removes_payloads_keeps_evidence(self):
        s = promo.promote_batch(self.root, self.batch, reclaim=True, log=lambda *_: None)
        self.assertEqual(s["promoted"], 1)
        self.assertFalse((self.attempt / "original.parquet").exists())
        self.assertFalse((self.attempt / "repaired.parquet").exists())
        self.assertTrue((self.attempt / "validation.json").exists())
        self.assertTrue((self.county / "complete.json").exists())
        marker = json.loads((self.county / "promoted.json").read_text(encoding="utf-8"))
        self.assertGreater(marker["reclaimed_bytes"], 0)
        # Re-run after reclaim: still idempotent, no refusal because hash already matches.
        s2 = promo.promote_batch(self.root, self.batch, reclaim=True, log=lambda *_: None)
        self.assertEqual(s2["already_promoted"], 1, s2)

    def test_fips_filter_rejects_unknown(self):
        with self.assertRaises(promo.PromotionRefused):
            promo.promote_batch(self.root, self.batch, fips=["99002"], log=lambda *_: None)

    def test_canonical_outside_normalized_refused(self):
        rogue = self.root / "elsewhere.parquet"
        shutil.copy2(self.canonical, rogue)
        self.report["identity"]["source"] = str(rogue.resolve())
        self._save_report()
        s = promo.promote_batch(self.root, self.batch, log=lambda *_: None)
        self.assertEqual(s["refused"], 1)
        self.assertIn("Unsafe path", s["counties"]["99001"]["error"])


if __name__ == "__main__":
    unittest.main(verbosity=1)
