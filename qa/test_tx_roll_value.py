"""TX CAD-roll value driver (recon/tx_roll_value.py): co-owner shares are SUMMED to the property's
full market value (the Pandai roll prorates Market_Value by Interest), roll-link discovery picks the
newest certified roll YEAR parsed from the file name, a join whose owners disagree is refused, the
prior certified roll backs a fallback + year-over-year hold, and --replace rebuilds only values
this driver wrote. Offline: in-memory rolls, stubbed HTTP, temp dirs for every path written."""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "recon"))
import tx_roll_value as T  # noqa: E402

COLS = ["Jurisdiction", "Parcel_ID", "Account", "Ownership_Sequence", "Interest", "Name", "Market_Value"]
SPEC = {"base": "https://x", "join": "parcel_id", "county": "Test"}


def roll(rows):
    return pd.DataFrame(rows, columns=COLS, dtype=str)


def roll_csv(values, names=None):
    """{apn: value} -> roll CSV bytes, one single-owner row per parcel."""
    names = names or {}
    return roll([["00", k, f"A{k}", "1", "1.0", names.get(k, f"OWNER {k}"), str(v)]
                 for k, v in values.items()]).to_csv(index=False).encode()


class Resp:
    def __init__(self, text="", url="", content=b""):
        self.text, self.url, self.content = text, url, content

    def raise_for_status(self):
        pass


def stub_rolls(rolls):
    """Patch discovery + download to serve {year: csv bytes}; the newest year when unpinned."""
    def discover(base, year=None):
        yr = int(year) if year else max(rolls)
        if yr not in rolls:
            raise RuntimeError(f"no {yr} certified-roll CSV link")
        return f"https://x/{yr}.csv", yr

    def get(url, **_):
        return Resp(content=rolls[int(url.rsplit("/", 1)[1][:4])])
    return (mock.patch.object(T, "discover_roll_url", side_effect=discover),
            mock.patch.object(T.requests, "get", side_effect=get))


class RollValuesTest(unittest.TestCase):
    def test_owner_shares_summed_once(self):
        df = roll([
            ["00", "1", "A1", "1", "1.000000", "SOLE OWNER", "100000"],
            # 60/40 undivided interest, repeated for a second jurisdiction: one copy per share
            ["00", "2", "A2", "1", "0.600000", "OWNER A", "60000"],
            ["00", "2", "A2", "2", "0.400000", "OWNER B", "40000"],
            ["01", "2", "A2", "1", "0.600000", "OWNER A", "60000"],
            ["01", "2", "A2", "2", "0.400000", "OWNER B", "40000"],
            ["00", "3", "A3", "1", "0.333333", "HEIR 1", "50000"],
            ["00", "3", "A3", "2", "0.333333", "HEIR 2", "50000"],
            ["00", "3", "A3", "3", "0.333334", "HEIR 3", "50000"],
            # a co-owner's own account: its value is already that share, kept as-is
            ["00", "4", "A4", "1", "0.500000", "HALF OWNER", "25000"],
            ["00", "5", "A5", "1", "1.000000", "EXEMPT", "0"],
            ["00", "", "A6", "1", "1.000000", "NO KEY", "999"],
        ])
        out, rows, st = T.roll_values(df, "Parcel_ID")
        self.assertEqual(dict(zip(out["apn"], out["assessed_value"])),
                         {"1": 100000, "2": 100000, "3": 150000, "4": 25000})
        self.assertEqual(st["multi_owner"], 2)
        self.assertEqual(st["vs_first_row"], 140000)  # what keeping the first row would have lost
        self.assertEqual(st["interest_off"], 1)
        self.assertEqual((st["dup_shares"], st["jur_conflicts"]), (0, 0))
        self.assertNotIn("", set(rows["_key"]))

    def test_account_key(self):
        df = roll([["00", "7", "R000123", "1", "1.0", "X", "5,000"]])
        out, _, _ = T.roll_values(df, "Account")
        self.assertEqual(out["apn"].tolist(), ["R000123"])
        self.assertEqual(out["assessed_value"].tolist(), [5000])


class DiscoverTest(unittest.TestCase):
    PAGE = "".join(
        f"<a href='/Forms/ExcelDownload?subPath=Data%20Records&fileName={n}'>x</a>" for n in (
            "1752170657_2025+X+CAD+Certified+Appraisal+Roll+-+Real-Personal++-+Excel.csv",
            "1784305783_2026+X+CAD+Certified+Appraisal+Roll+-+Real-Personal++-+Excel.csv",
            # older roll re-uploaded later: newest STAMP, but not the newest roll year
            "1799999999_2024+X+CAD+Certified+Appraisal+Roll+-+Excel.csv",
            # upload stamp contains '2025' — the old substring test picked this for --year 2025
            "1782025000_2023+X+CAD+Certified+Appraisal+Roll+-+Excel.csv",
            "1790000000_2026+X+CAD+Supplement+Roll.csv",
            # mineral roll uploaded AFTER the real-personal roll of the same year (Chambers 2025)
            "1795000000_2025+X+County+Certified+Mineral+Appraisal+Roll.csv",
        ))

    def discover(self, year=None, url="https://x.org/home/DataRecords"):
        with mock.patch.object(T.requests, "get", return_value=Resp(self.PAGE, url)):
            return T.discover_roll_url("https://x.org", year)

    def test_newest_roll_year_wins(self):
        url, yr = self.discover()
        self.assertEqual(yr, 2026)
        self.assertIn("1784305783_2026", url)
        self.assertTrue(url.startswith("https://x.org/Forms/ExcelDownload"))

    def test_year_pins_vintage_by_file_name(self):
        url, yr = self.discover("2025")
        self.assertEqual(yr, 2025)
        self.assertIn("1752170657_2025", url)
        with self.assertRaises(RuntimeError):
            self.discover("2021")

    def test_lockout_page_is_an_error(self):
        with self.assertRaisesRegex(RuntimeError, "locked out"):
            self.discover(url="https://x.org/Home/UserLockedOut")


class CountyCase(unittest.TestCase):
    """One fake county in a temp dir: normalized file, optional old overlay, stubbed rolls."""
    FIPS = "99002"
    OWNERS = {"1": "A OWNER", "2": "B OWNER", "3": "C OWNER", "4": "D OWNER"}

    def run_case(self, rolls, values=(0, 0, 0, 0), overlay=None, dry=True, replace=False, cache=None):
        tmp = self.enterContext(tempfile.TemporaryDirectory())
        nf = os.path.join(tmp, f"{self.FIPS}_test.parquet")
        pd.DataFrame({"apn": list(self.OWNERS), "assessed_value": list(values),
                      "owner": list(self.OWNERS.values())}).to_parquet(nf, index=False)
        ovp = os.path.join(tmp, f"{self.FIPS}.parquet")
        if overlay:
            pd.DataFrame({"apn": list(overlay), "assessed_value": [float(v) for v in overlay.values()],
                          "land_value": float("nan"), "improvement_value": float("nan"),
                          "value_source": "tx_cad_roll_2025"}).to_parquet(ovp, index=False)
        rolls = {yr: roll_csv(v, self.OWNERS) for yr, v in rolls.items()}
        disc, get = stub_rolls(rolls)
        with mock.patch.object(T, "NORM", tmp), mock.patch.object(T.VO, "NORM", tmp), \
                mock.patch.object(T.VO, "OVERLAY", tmp), \
                mock.patch.object(T, "MANIFEST", os.path.join(tmp, "manifest.json")), disc, get:
            st = T.process(self.FIPS, SPEC, dry=dry, replace=replace, cache=cache)
        vals = pd.read_parquet(nf)["assessed_value"].tolist()
        ov = pd.read_parquet(ovp) if os.path.exists(ovp) else None
        man, note = os.path.join(tmp, "manifest.json"), None
        if os.path.exists(man):
            with open(man, encoding="utf-8") as f:
                note = json.load(f)[self.FIPS]["note"]
        return st, vals, ov, note


class JoinGuardTest(CountyCase):
    def test_matching_owners_pass(self):
        st, vals, ov, _ = self.run_case({2025: {"1": 1000, "2": 1000, "3": 1000},
                                         2026: {"1": 1000, "2": 1000, "3": 1000}})
        self.assertEqual((st["status"], st["owner_agree"], st["fill_rows"]), ("dry-ok", 1.0, 3))
        self.assertEqual((vals, ov), ([0, 0, 0, 0], None))  # dry run wrote nothing

    def test_same_ids_different_owners_refused(self):
        rolls = {yr: roll_csv({"1": 1000, "2": 1000, "3": 1000},
                              {"1": "ALPHA LLC", "2": "BETA TRUST", "3": "GAMMA INC"}) for yr in (2025, 2026)}
        tmp = self.enterContext(tempfile.TemporaryDirectory())
        pd.DataFrame({"apn": ["1", "2", "3"], "assessed_value": [0, 0, 0],
                      "owner": ["SMITH JOHN", "DOE JANE", "ROE RICHARD"]}
                     ).to_parquet(os.path.join(tmp, "99001_test.parquet"), index=False)
        disc, get = stub_rolls(rolls)
        with mock.patch.object(T, "NORM", tmp), mock.patch.object(T.VO, "OVERLAY", tmp), disc, get:
            st = T.process("99001", SPEC, dry=True)
        self.assertEqual((st["status"], st["owner_agree"]), ("owner<50%", 0.0))


class FallbackTest(CountyCase):
    # prior total 1,002,200: apn 1 jumps 20x but is 2% of the county -> written; apn 3 jumps
    # 1,200x to 60% of the county -> held at 500; apn 4 is missing from 2026 -> 700
    ROLLS = {2025: {"1": 1000, "2": 1_000_000, "3": 500, "4": 700},
             2026: {"1": 20_000, "2": 1_000_000, "3": 600_000}}

    def test_dry_run_counts(self):
        st, vals, ov, _ = self.run_case(self.ROLLS)
        self.assertEqual((st["status"], st["prior_year"], st["fallback_missing"], st["fallback_held"],
                          st["held_excess"], st["fill_rows"]), ("dry-ok", 2025, 1, 1, 599_500, 4))
        rv = st["_review"].set_index("apn")
        self.assertEqual(rv.loc["3", "reason"], "held")
        self.assertEqual(rv.loc["4", "reason"], "missing")
        self.assertEqual(vals, [0, 0, 0, 0])

    def test_write_labels_every_value(self):
        st, vals, ov, note = self.run_case(self.ROLLS, dry=False)
        self.assertEqual(st["status"], "written")
        self.assertEqual(vals, [20_000, 1_000_000, 500, 700])
        self.assertEqual(dict(zip(ov["apn"], ov["value_source"])),
                         {"1": "tx_cad_roll_2026", "2": "tx_cad_roll_2026",
                          "3": "tx_cad_roll_2025_held", "4": "tx_cad_roll_2025"})
        self.assertIn("2025 roll fallback: 1 missing, 1 held", note)

    def test_no_prior_roll_no_write(self):
        with self.assertRaisesRegex(RuntimeError, "no 2025"):
            self.run_case({2026: self.ROLLS[2026]}, dry=False)

    def test_cache_serves_the_dry_runs_files(self):
        cache = self.enterContext(tempfile.TemporaryDirectory())
        self.run_case(self.ROLLS, cache=cache)
        with mock.patch.object(T.requests, "get", side_effect=AssertionError("network")), \
                mock.patch.object(T, "discover_roll_url", side_effect=AssertionError("network")):
            tmp = self.enterContext(tempfile.TemporaryDirectory())
            nf = os.path.join(tmp, f"{self.FIPS}_test.parquet")
            pd.DataFrame({"apn": list(self.OWNERS), "assessed_value": [0, 0, 0, 0],
                          "owner": list(self.OWNERS.values())}).to_parquet(nf, index=False)
            with mock.patch.object(T, "NORM", tmp), mock.patch.object(T.VO, "OVERLAY", tmp):
                st = T.process(self.FIPS, SPEC, dry=True, cache=cache)
        self.assertEqual((st["status"], st["fallback_held"], st["fill_rows"]), ("dry-ok", 1, 4))


class ReplaceTest(CountyCase):
    """--replace on a county this driver valued from 2025 = {1:100, 2:200, 4:300}; the 2026 roll
    = {1:150, 2:200, 3:50}: 1 changes, 2 same, 3 newly filled, 4 falls back to its 2025 value."""
    OLD = {"1": 100, "2": 200, "4": 300}
    ROLLS = {2025: OLD, 2026: {"1": 150, "2": 200, "3": 50}}

    def run_replace(self, **kw):
        kw.setdefault("values", (100, 200, 0, 300))
        return self.run_case(self.ROLLS, overlay=self.OLD, replace=True, **kw)

    def test_dry_run_counts_and_writes_nothing(self):
        st, vals, ov, note = self.run_replace()
        self.assertEqual(st["status"], "dry-ok")
        self.assertEqual((st["replace_changed"], st["replace_dropped"], st["fill_rows"],
                          st["source_positive"], st["replaced_usd"], st["fallback_missing"]),
                         (1, 0, 1, 0, 50.0, 1))
        self.assertEqual(vals, [100, 200, 0, 300])
        self.assertEqual(set(ov["value_source"]), {"tx_cad_roll_2025"})
        self.assertIsNone(note)

    def test_write_rebuilds_from_new_roll(self):
        st, vals, ov, note = self.run_replace(dry=False)
        self.assertEqual(st["status"], "written")
        self.assertEqual(vals, [150, 200, 50, 300])
        self.assertEqual(dict(zip(ov["apn"], ov["value_source"])),
                         {"1": "tx_cad_roll_2026", "2": "tx_cad_roll_2026", "3": "tx_cad_roll_2026",
                          "4": "tx_cad_roll_2025"})
        self.assertIn("replaced tx_cad_roll_2025", note)

    def test_value_in_neither_roll_resets_to_source_zero(self):
        rolls = {2025: {"1": 100, "2": 200}, 2026: {"1": 150, "2": 200, "3": 50}}
        st, vals, ov, _ = self.run_case(rolls, values=(100, 200, 0, 300), overlay=self.OLD,
                                        replace=True, dry=False)
        self.assertEqual((st["status"], st["replace_dropped"]), ("written", 1))
        self.assertEqual(vals, [150, 200, 50, 0])

    def test_source_value_blocks_replace(self):
        # apn 4 = 999, not the 300 this driver wrote: a source value, never overwritten
        st, vals, ov, _ = self.run_replace(values=(100, 200, 0, 999), dry=False)
        self.assertEqual((st["status"], st["source_positive"]), ("replace-refused", 1))
        self.assertEqual(vals, [100, 200, 0, 999])
        self.assertEqual(set(ov["value_source"]), {"tx_cad_roll_2025"})

    def test_plain_write_refused_when_overlay_exists(self):
        st, vals, ov, _ = self.run_case(self.ROLLS, values=(100, 200, 0, 300), overlay=self.OLD,
                                        dry=False)
        self.assertEqual(st["status"], "overlay-exists")
        self.assertEqual(vals, [100, 200, 0, 300])
        self.assertEqual(set(ov["value_source"]), {"tx_cad_roll_2025"})

    def test_replace_needs_driver_overlay(self):
        with self.assertRaisesRegex(RuntimeError, "needs an existing overlay"):
            self.run_case(self.ROLLS, replace=True)


if __name__ == "__main__":
    unittest.main()
