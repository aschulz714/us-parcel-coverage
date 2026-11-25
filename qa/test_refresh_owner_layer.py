"""recon/refresh_owner_layer.py: the backup/retry moves, and the SQL twin of the owners_std
placeholder rules (which picks the county files to rewrite) agreeing with the Python rules.
Offline: temp dirs and in-memory strings only."""
import os
import sys
import tempfile
import unittest

import duckdb
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "recon"))
sys.path.insert(0, os.path.join(ROOT, "src"))
import owners_std  # noqa: E402
import refresh_owner_layer as rol  # noqa: E402


class BackupTest(unittest.TestCase):
    def test_first_move_then_retry_semantics(self):
        with tempfile.TemporaryDirectory() as tmp:
            own, bak = os.path.join(tmp, "owners"), os.path.join(tmp, "backup")
            os.makedirs(os.path.join(own, "graph", "_extract"))
            open(os.path.join(own, "owner_summary.parquet"), "w").write("old")
            open(os.path.join(own, "graph", "_extract", "WA.parquet"), "w").write("old")
            saved = rol.OWN
            rol.OWN = own
            try:
                rel = ["owner_summary.parquet", "graph/_extract", "missing.parquet"]
                rol.backup(rel, bak)
                self.assertFalse(os.path.exists(os.path.join(own, "owner_summary.parquet")))
                self.assertEqual(open(os.path.join(bak, "owner_summary.parquet")).read(), "old")
                self.assertTrue(os.path.exists(os.path.join(bak, "graph", "_extract", "WA.parquet")))
                # a retried step: this run's partial file is dropped, its resumable dir is kept,
                # and the original backup is never overwritten
                open(os.path.join(own, "owner_summary.parquet"), "w").write("partial")
                os.makedirs(os.path.join(own, "graph", "_extract"))
                open(os.path.join(own, "graph", "_extract", "OR.parquet"), "w").write("new")
                rol.backup(rel, bak)
            finally:
                rol.OWN = saved
            self.assertFalse(os.path.exists(os.path.join(own, "owner_summary.parquet")))
            self.assertEqual(open(os.path.join(bak, "owner_summary.parquet")).read(), "old")
            self.assertTrue(os.path.exists(os.path.join(own, "graph", "_extract", "OR.parquet")))
            self.assertFalse(os.path.exists(os.path.join(bak, "graph", "_extract", "OR.parquet")))


class PlaceholderSqlTest(unittest.TestCase):
    def test_sql_selects_exactly_what_python_brackets(self):
        # stored owner_norm values as the OLD rules left them
        stored = ["INFORMATION ONLY", "INFORMATION ONLY SAN DIEGO UNIFIED PORT DISTRICT", "STREET",
                  "STREET LLC", "LISTED WITH 36123016", "LISTED WITH 10126401 TO 403",
                  "SEE 03 001 280 00 FOR 2009", "SEE JOHN & MARY", "0", "&", "0 0 & 0", "3M CO",
                  "SMITH JOHN", "NOT AVAILABLE", "[UNKNOWN OWNER - source placeholder]"]
        py = owners_std.normalize_series(pd.Series(stored, dtype=object), {})
        want = {s for s, n in zip(stored, py) if n.startswith("[") and not s.startswith("[")}
        con = duckdb.connect()
        got = {r[0] for r in con.execute(
            f"select owner_norm from unnest(?) t(owner_norm) where {rol.placeholder_where()}",
            [stored]).fetchall()}
        self.assertEqual(got, want)
        self.assertIn("NOT AVAILABLE", got)  # an older exact sentinel left unbracketed is caught too


if __name__ == "__main__":
    unittest.main()
