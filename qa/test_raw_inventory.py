"""Offline tests: raw_inventory local recording + validate.tier_c_shortfall evidence tiers."""
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "qa"))
import raw_inventory  # noqa: E402
import validate  # noqa: E402


def write_rows(path, n):
    pq.write_table(pa.table({"apn": [f"A{i}" for i in range(n)]}), path)


class RecorderReport:
    def __init__(self):
        self.fails, self.warns, self.infos = [], [], []

    def fail(self, tier, msg):
        self.fails.append(msg)

    def warn(self, tier, msg):
        self.warns.append(msg)

    def info(self, msg):
        self.infos.append(msg)


class RawInventoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="rawinv-"))
        self.saved = (raw_inventory.INVENTORY, validate.RAW_INVENTORY)
        raw_inventory.INVENTORY = str(self.tmp / "raw_inventory.json")
        validate.RAW_INVENTORY = raw_inventory.INVENTORY

    def tearDown(self):
        raw_inventory.INVENTORY, validate.RAW_INVENTORY = self.saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_record_local_file(self):
        p = self.tmp / "99001_test.parquet"
        write_rows(str(p), 123)
        e = raw_inventory.record(str(p))
        self.assertEqual(e["rows"], 123)
        self.assertEqual(e["source"], "local")
        self.assertEqual(len(e["sha256"]), 64)
        self.assertEqual(raw_inventory.load()["99001_test.parquet"]["rows"], 123)

    def test_remote_footer_parse_matches_local(self):
        # emulate the ranged GET: only the tail of the file is handed to the parser
        p = self.tmp / "99002_test.parquet"
        write_rows(str(p), 4567)
        data = p.read_bytes()
        tail = data[-min(len(data), 4096):]
        flen = int.from_bytes(tail[-8:-4], "little")
        if flen + 8 > len(tail):
            tail = data[-(flen + 8):]
        self.assertEqual(pq.read_metadata(pa.BufferReader(tail)).num_rows, 4567)

    def _shortfall(self, ref, raw_rows=None, inv_rows=None, norm_rows=100):
        norm = self.tmp / "99003_test.parquet"
        write_rows(str(norm), norm_rows)
        raw = self.tmp / "raw" / "99003_test.parquet"
        raw.parent.mkdir(exist_ok=True)
        if raw_rows is not None:
            write_rows(str(raw), raw_rows)
        if inv_rows is not None:
            Path(raw_inventory.INVENTORY).write_text(json.dumps({"99003_test.parquet": {"rows": inv_rows}}))
        manifest = {"99003": {"normalized_file": str(norm), "file": str(raw), "reported_count": ref}}
        rep = RecorderReport()
        validate.tier_c_shortfall({"ZZ": {"files": [str(norm)]}}, manifest, rep, {})
        return rep

    def test_local_raw_is_first_evidence(self):
        rep = self._shortfall(ref=100, raw_rows=100, inv_rows=50, norm_rows=90)
        self.assertEqual(rep.fails, [], rep.fails)

    def test_archived_inventory_is_second_evidence(self):
        rep = self._shortfall(ref=100, raw_rows=None, inv_rows=100, norm_rows=90)
        self.assertEqual(rep.fails, [])

    def test_normalized_rows_are_last_resort_and_match_passes(self):
        rep = self._shortfall(ref=100, raw_rows=None, inv_rows=None, norm_rows=100)
        self.assertEqual(rep.fails, [])

    def test_missing_raw_is_not_itself_a_failure_but_shortfall_is(self):
        rep = self._shortfall(ref=100, raw_rows=None, inv_rows=None, norm_rows=80)
        self.assertEqual(len(rep.fails), 1)
        self.assertIn("normalized rows only 80", rep.fails[0])
        self.assertIn("delta -20", rep.fails[0])
        self.assertNotIn("artifact missing", " ".join(rep.fails))

    def test_archived_shortfall_fails(self):
        rep = self._shortfall(ref=100, raw_rows=None, inv_rows=95, norm_rows=100)
        self.assertEqual(len(rep.fails), 1)
        self.assertIn("archived raw (inventory) 95", rep.fails[0])


if __name__ == "__main__":
    unittest.main(verbosity=1)
