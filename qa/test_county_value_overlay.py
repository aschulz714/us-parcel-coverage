"""National county value overlays (recon/county_value_overlay.py): key transforms bring our apn
and the source key together, pulls refuse short / overlapping pages, polygon pieces must agree,
and values go through the TX drivers' shared gates with a county_roll_<year> label.
Offline: stubbed ArcGIS responses, temp dirs for every path written."""
import json
import re
import os
import sys
import tempfile
import unittest
from unittest import mock

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "recon"))
import county_value_overlay as C  # noqa: E402

R = C.R


class KeyTest(unittest.TestCase):
    def test_transforms(self):
        self.assertEqual(C.KEYS["yavapai"]("1310321188007"), "10321188")
        self.assertEqual(C.KEYS["yavapai"]("1330113574015"), "30113574A")  # split 01 -> A
        self.assertIsNone(C.KEYS["yavapai"]("R12345"))
        self.assertEqual(C.KEYS["collapse_ws"]("17 0211  LL3491 "), "17 0211 LL3491")
        self.assertEqual(C.KEYS["md_020"]("354016749050"), "020354016749050")


def fake_server(rows, count=None, repeat_oid=False, fields_out=None):
    """_getj stub: meta, count and ordered pages over `rows` (list of attribute dicts)."""
    def getj(sess, url, params, **_):
        if params.get("f") == "json" and "where" not in params:
            return {"fields": [{"name": "OBJECTID", "type": "esriFieldTypeOID"}],
                    "maxRecordCount": 2}, b""
        if params.get("returnCountOnly"):
            return {"count": len(rows) if count is None else count}, b""
        m = re.search(r"OBJECTID > (\d+)", params["where"])   # keyset paging
        after = int(m.group(1)) if m else 0
        page = [dict(r) for r in rows if r["OBJECTID"] > after][:params["resultRecordCount"]]
        if repeat_oid and after == 2 and page:
            page[0]["OBJECTID"] = 1
        if fields_out is not None:
            page = [{k: v for k, v in r.items() if k in fields_out} for r in page]
        return {"features": [{"attributes": r} for r in page]}, b""
    return mock.patch.object(C.I, "_getj", side_effect=getj)


class PullTest(unittest.TestCase):
    ROWS = [{"OBJECTID": i, "PIN": f"P{i}", "VAL": 100 * i} for i in range(1, 6)]

    def test_full_pull_in_pages(self):
        with fake_server(self.ROWS):
            df = C.arcgis_rows("https://x/0", ["PIN", "VAL"])
        self.assertEqual((list(df.columns), len(df)), (["PIN", "VAL"], 5))

    def test_short_or_overlapping_pull_refused(self):
        with fake_server(self.ROWS, count=6), self.assertRaisesRegex(RuntimeError, "incomplete"):
            C.arcgis_rows("https://x/0", ["PIN", "VAL"])
        with fake_server(self.ROWS, repeat_oid=True), self.assertRaisesRegex(RuntimeError, "repeated"):
            C.arcgis_rows("https://x/0", ["PIN", "VAL"])

    def test_missing_field_refused(self):
        with fake_server(self.ROWS, fields_out={"OBJECTID", "PIN"}), \
                self.assertRaisesRegex(RuntimeError, "not returned"):
            C.arcgis_rows("https://x/0", ["PIN", "VAL"])

    def test_cache_serves_the_same_rows(self):
        cache = self.enterContext(tempfile.TemporaryDirectory())
        with fake_server(self.ROWS):
            a = C.arcgis_rows("https://x/0", ["PIN", "VAL"], cache=cache)
        with mock.patch.object(C.I, "_getj", side_effect=AssertionError("network")):
            b = C.arcgis_rows("https://x/0", ["PIN", "VAL"], cache=cache)
        self.assertTrue(a.equals(b))


class SocrataTest(unittest.TestCase):
    ROWS = [{"acct": f"A{i}", "v": str(10 * i)} for i in range(5)]

    def stub(self, count=None):
        def getj(sess, url, params, **_):
            if params["$select"].startswith("count("):
                return [{"n": str(len(self.ROWS) if count is None else count)}], b""
            off, lim = params["$offset"], params["$limit"]
            return self.ROWS[off:off + lim], b""
        return mock.patch.object(C.I, "_getj", side_effect=getj)

    def test_paged_pull_and_short_pull_refused(self):
        with self.stub():
            df = C.socrata_rows("https://x/resource/abcd.json", ["acct", "v"], page=2)
        self.assertEqual((len(df), list(df.columns)), (5, ["acct", "v"]))
        with self.stub(count=7), self.assertRaisesRegex(RuntimeError, "incomplete"):
            C.socrata_rows("https://x/resource/abcd.json", ["acct", "v"], page=2)


class LoadTest(unittest.TestCase):
    SPEC = {"county": "Test", "join": "PIN", "url": "u", "key": "PIN", "value": "TOT", "land": "LND",
            "imprv": "IMP", "owner": "OWN", "year": 2026, "year_field": "YR", "basis": "market",
            "prior": {"field": "PRV", "year": 2025}}

    def src(self, **over):
        d = pd.DataFrame({"PIN": ["A1", "A2", "A2", "A3", "B9"], "TOT": [300, 500, 500, 0, 900],
                          "LND": [100, 200, 200, 0, 400], "IMP": [200, 300, 300, 0, 500],
                          "OWN": ["SMITH JOHN", "DOE JANE", "DOE JANE", "X", "Y"],
                          "YR": [2026] * 5, "PRV": [280, 450, 450, 0, 800]})
        for k, v in over.items():
            d[k] = v
        return d

    def load(self, src, spec=None, ours=("A1", "A2", "A3", "Z0")):
        with mock.patch.object(C, "fetch", return_value=src):
            return C.load("99001", spec or self.SPEC, list(ours))

    def test_values_rows_and_same_record_prior(self):
        out, rows, prior, st = self.load(self.src())
        self.assertEqual(dict(zip(out["apn"], out["assessed_value"])), {"A1": 300, "A2": 500})
        self.assertEqual(dict(zip(prior["apn"], prior["assessed_value"])), {"A1": 280, "A2": 450})
        self.assertEqual(set(rows.columns), {"_key", "Name"})
        self.assertEqual((st["matched"], st["dup_conflicts"], st["sum_check"]), (3, 0, 1.0))

    def test_disagreeing_pieces_dropped_or_refused(self):
        # 1 conflicting key among 200: dropped and counted
        n = 200
        big = pd.DataFrame({"PIN": [f"K{i}" for i in range(n)] + ["K0"], "TOT": [100] * n + [999],
                            "LND": [40] * n + [400], "IMP": [60] * n + [599], "OWN": "X",
                            "YR": 2026, "PRV": 90})
        out, _, _, st = self.load(big, ours=[f"K{i}" for i in range(n)])
        self.assertEqual((st["dup_conflicts"], len(out), "K0" in set(out["apn"])), (1, n - 1, False))
        # 1 of 4 keys: the grain is not what we think -> refuse
        with self.assertRaisesRegex(RuntimeError, "disagreeing values"):
            self.load(self.src(TOT=[300, 500, 510, 0, 900], IMP=[200, 300, 310, 0, 500]))

    def test_wrong_fields_or_year_refused(self):
        with self.assertRaisesRegex(RuntimeError, "wrong fields"):
            self.load(self.src(LND=[1, 1, 1, 0, 1]))
        with self.assertRaisesRegex(RuntimeError, "!= 2026"):
            self.load(self.src(YR=[2025] * 5))

    def test_key_spread_over_several_fields(self):
        # Baltimore City: county '03' + ward '03' + account '081435 059' (trailing blanks) = our apn
        spec = {"county": "BC", "join": "k", "url": "u", "key": ["CNTY", "WARD", "ACCT"],
                "value": ["LND", "IMP"], "land": "LND", "imprv": "IMP", "year": 2026, "basis": "m",
                "no_prior": "x"}
        src = pd.DataFrame({"CNTY": ["03", "03"], "WARD": ["03", "26"], "ACCT": ["081435 059   ", "405928 005"],
                            "LND": ["100", "50"], "IMP": ["200", "0"]})
        out, _, prior, st = self.load(src, spec, ours=["0303081435 059", "0326405928 005"])
        self.assertEqual(dict(zip(out["apn"], out["assessed_value"])),
                         {"0303081435 059": 300, "0326405928 005": 50})
        self.assertIsNone(prior)

    def test_no_owner_source_skips_the_owner_check(self):
        spec = {k: v for k, v in self.SPEC.items() if k != "owner"}
        _, rows, _, _ = self.load(self.src(), spec)
        self.assertNotIn("Name", rows.columns)


class ProcessTest(unittest.TestCase):
    FIPS = "99004"
    SPEC = {"county": "Test County", "join": "PIN", "url": "https://x/rest/services/T/0", "key": "PIN",
            "value": "TOT", "year": 2026, "basis": "market value",
            "prior": {"url": "https://x/rest/services/P/0", "key": "PIN", "value": "TOT", "year": 2025}}

    def run_case(self, spec, cur, prior=None):
        tmp = self.enterContext(tempfile.TemporaryDirectory())
        nf = os.path.join(tmp, f"{self.FIPS}_test.parquet")
        pd.DataFrame({"apn": ["A1", "A2", "A3"], "assessed_value": [0, 0, 0],
                      "owner": ["SMITH", "DOE", "ROE"]}).to_parquet(nf, index=False)
        man = os.path.join(tmp, "manifest.json")

        def rows(url, fields, where="1=1", cache=None):
            return prior if "/P/" in url else cur
        with mock.patch.object(C, "arcgis_rows", side_effect=rows), \
                mock.patch.object(R, "NORM", tmp), mock.patch.object(R.VO, "NORM", tmp), \
                mock.patch.object(R.VO, "OVERLAY", tmp), mock.patch.object(R, "MANIFEST", man):
            st = C.process(self.FIPS, spec)
        ov = pd.read_parquet(os.path.join(tmp, f"{self.FIPS}.parquet"))
        note = json.load(open(man, encoding="utf-8"))[self.FIPS]["note"]
        return st, pd.read_parquet(nf)["assessed_value"].tolist(), ov, note

    def test_write_with_separate_prior_layer(self):
        cur = pd.DataFrame({"PIN": ["A1", "A2"], "TOT": [5000, 7000]})
        prior = pd.DataFrame({"PIN": ["A1", "A2", "A3"], "TOT": [4800, 6900, 900]})
        st, vals, ov, note = self.run_case(self.SPEC, cur, prior)
        self.assertEqual((st["status"], vals), ("written", [5000, 7000, 900]))
        self.assertEqual(dict(zip(ov["apn"], ov["value_source"])),
                         {"A1": "county_roll_2026", "A2": "county_roll_2026", "A3": "county_roll_2025"})
        self.assertTrue(note.startswith("Test County 2026 value overlay: market value "))
        self.assertIn("2025 roll fallback: 1 missing, 0 held", note)

    def test_no_prior_county_holds_out_a_material_value(self):
        spec = {k: v for k, v in self.SPEC.items() if k != "prior"}
        spec["no_prior"] = "single-vintage state layer"
        cur = pd.DataFrame({"PIN": ["A1", "A2", "A3"], "TOT": [5000, 5000, 900000]})
        st, vals, ov, note = self.run_case(spec, cur)
        self.assertEqual((st["status"], vals), ("written", [5000, 5000, 0]))
        self.assertIn("no prior roll published (single-vintage state layer)", note)

    def test_prior_or_reason_required(self):
        spec = {k: v for k, v in self.SPEC.items() if k != "prior"}
        with self.assertRaisesRegex(RuntimeError, "no prior roll configured"):
            C.process(self.FIPS, spec, dry=True)


if __name__ == "__main__":
    unittest.main()
