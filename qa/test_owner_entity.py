"""owner_entity v2 name rule (review finding #12): identity numbers (a leading numeric run, or a
number after an ID designator) are never stripped; series numbers still are; a core made only
of generic words falls back to the exact name; names with no letters get no entity.
Offline: runs the production SQL macros on in-memory strings and a synthetic owner_summary."""
import os
import sys
import tempfile
import unittest

import duckdb
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
import owner_entity as oe  # noqa: E402


def entity_names(names):
    con = duckdb.connect()
    oe.register_macros(con)
    rows = con.execute("select n, entity_name(n) from unnest(?) t(n)", [names]).fetchall()
    return dict(rows)


class EntityNameRuleTest(unittest.TestCase):
    def assert_separate(self, *names):
        got = entity_names(list(names))
        self.assertEqual(len(set(got.values())), len(names), got)

    def assert_merged(self, expected, *names):
        got = entity_names(list(names))
        self.assertEqual(set(got.values()), {expected}, got)

    def test_review_repro_address_llcs_stay_separate(self):
        # Sept-6 review: v1 mapped both to 'MAIN STREET'
        got = entity_names(["123 MAIN STREET LLC", "456 MAIN STREET LLC"])
        self.assertEqual(got, {"123 MAIN STREET LLC": "123 MAIN STREET",
                               "456 MAIN STREET LLC": "456 MAIN STREET"})

    def test_leading_numbers_are_identity(self):
        self.assert_separate("007 REALTY LLC", "01150 REALTY LLC", "844 846 REALTY LLC")
        self.assert_separate("6058 PROPERTIES LLC", "9456 PROPERTIES LLC")
        self.assert_separate("1000014517 ONTARIO INC", "0402771 ONTARIO LTD")
        self.assert_separate("31 43 81 STREET LLC", "2238 86 STREET LLC")
        self.assert_separate("206 STATE LLC", "2535 STATE LLC")

    def test_designator_numbers_are_identity(self):
        self.assert_separate("HARRIS COUNTY MUD NO 501", "HARRIS COUNTY MUD NO 168")
        self.assert_separate("FIDELITY NATIONAL TITLE AGENCY INC TR 60568",
                             "FIDELITY NATIONAL TITLE AGENCY INC TR 60558")
        self.assert_separate("SCHOOL DISTRICT 60", "SCHOOL DISTRICT 89")
        self.assert_separate("AMERICAN LEGION POST 12", "AMERICAN LEGION POST 45")
        self.assert_separate("ROUTE 66 CAR WASH LLC", "ROUTE 9 CAR WASH LLC")
        self.assert_separate("1 ROUTE 20 LLC", "1 ROUTE 6 LLC")
        self.assert_separate("EQUITY TRUST CO CUSTODIAN 20020", "EQUITY TRUST CO CUSTODIAN 50")
        self.assert_separate("SMITH FAMILY TRUST 12 15 2005", "SMITH FAMILY TRUST 3 1 1999")
        self.assert_separate("EQUITY TRUST CO CUSTODIAN FBO 53603 IRA",
                             "EQUITY TRUST CO CUSTODIAN FBO 101713 IRA")
        self.assert_separate("CTLTC 000000126479", "CTLTC 000000128951")

    def test_generic_core_left_by_a_stripped_number_keeps_full_name(self):
        got = entity_names(["REALTY 12 LLC", "REALTY 14 LLC", "CEMETERY 10", "CEMETERY 5",
                            "NORTH 40 LLC", "BOARD OF EDUCATION 23", "STREET ON BOOK 12 PAGE 34"])
        self.assertEqual(got["REALTY 12 LLC"], "REALTY 12 LLC")
        self.assertEqual(got["CEMETERY 10"], "CEMETERY 10")
        self.assertEqual(got["NORTH 40 LLC"], "NORTH 40 LLC")
        self.assertEqual(got["BOARD OF EDUCATION 23"], "BOARD OF EDUCATION 23")
        self.assertEqual(got["STREET ON BOOK 12 PAGE 34"], "STREET ON BOOK 12 PAGE 34")
        self.assertNotEqual(got["REALTY 12 LLC"], got["REALTY 14 LLC"])
        self.assertNotEqual(got["CEMETERY 10"], got["CEMETERY 5"])

    def test_suffix_variants_of_an_all_generic_brand_merge(self):
        # no number was stripped, so the generic guard does not apply
        self.assert_merged("PUBLIC STORAGE", "PUBLIC STORAGE INC", "PUBLIC STORAGE")
        self.assert_merged("NATIONAL RETAIL PROPERTIES", "NATIONAL RETAIL PROPERTIES INC",
                           "NATIONAL RETAIL PROPERTIES LP")

    def test_series_numbers_still_merge(self):
        self.assert_merged("PROGRESS RESIDENTIAL", "PROGRESS RESIDENTIAL BORROWER 2019 1 LLC",
                           "PROGRESS RESIDENTIAL BORROWER 12 LLC",
                           "PROGRESS RESIDENTIAL 2015 1 BORROWER")
        self.assert_merged("UNITED STATES OF AMERICA", "UNITED STATES OF AMERICA 3129",
                           "UNITED STATES OF AMERICA")
        self.assert_merged("TRICON SFR", "TRICON SFR 2024 2 BORROWER LLC",
                           "TRICON SFR 2023 2 BORROWER LLC")
        self.assert_merged("SFR JV", "SFR JV 2 2023 01 BORROWER LLC", "SFR JV 1 2022 1 BORROWER LLC")
        self.assert_merged("INVITATION HOMES", "INVITATION HOMES 1 LP", "INVITATION HOMES 7 L P")
        self.assert_merged("STATE OF TEXAS", "STATE OF TEXAS 09", "STATE OF TEXAS")
        self.assert_merged("DETROIT LAND BANK AUTHORITY", "DETROIT LAND BANK AUTHORITY 38425",
                           "DETROIT LAND BANK AUTHORITY 6354204")
        self.assert_merged("AH4R PROPERTIES", "AH4R PROPERTIES 2 LLC", "AH4R PROPERTIES LLC")

    def test_suffix_variants_merge(self):
        self.assert_merged("123 MAIN STREET", "123 MAIN STREET LLC", "123 MAIN STREET INC")
        self.assert_merged("D R HORTON", "D R HORTON INC", "D R HORTON")
        self.assert_merged("7 ELEVEN", "7 ELEVEN INC", "7 ELEVEN")

    def test_initials_joined_by_ampersand_are_not_a_suffix(self):
        # 'L & P' is two people's initials, not the spaced 'L P' (limited partnership) suffix
        got = entity_names(["L & P ACRES LLC", "THOMPSON JERRY L & P L", "L & P CHILDRENS TRUST",
                            "WESTWICK S L & L C TTEES", "SMITH HOLDINGS L P", "SMITH HOLDINGS L L C"])
        self.assertEqual(got["L & P ACRES LLC"], "L P ACRES")
        self.assertEqual(got["THOMPSON JERRY L & P L"], "THOMPSON JERRY L P L")
        self.assertEqual(got["L & P CHILDRENS TRUST"], "L P CHILDRENS")
        self.assertEqual(got["WESTWICK S L & L C TTEES"], "WESTWICK S L L C TTEES")
        self.assertEqual(got["SMITH HOLDINGS L P"], "SMITH HOLDINGS")
        self.assertEqual(got["SMITH HOLDINGS L L C"], "SMITH HOLDINGS")

    def test_short_core_guard_keeps_full_name(self):
        got = entity_names(["11300 IH 35 LLC", "2017 1 IH BORROWER LP"])
        self.assertEqual(got["11300 IH 35 LLC"], "11300 IH 35 LLC")
        self.assertEqual(got["2017 1 IH BORROWER LP"], "2017 1 IH BORROWER LP")

    def test_placeholders_get_no_entity(self):
        con = duckdb.connect()
        rows = dict(con.execute(
            f"select coalesce(owner_norm, '<null>'), coalesce({oe.KEEP_OWNER}, false) from unnest(?) "
            f"t(owner_norm)", [["0", "&", "0 0 & 0", "[UNKNOWN OWNER - source placeholder]", "",
                               None, "SMITH JOHN", "7 ELEVEN"]]).fetchall())
        self.assertEqual({k for k, keep in rows.items() if keep}, {"SMITH JOHN", "7 ELEVEN"})


class BuildFromSummaryTest(unittest.TestCase):
    def test_staged_build_writes_map_and_summary(self):
        summary = pd.DataFrame([
            # owner_norm, parcels, total_value, total_value_dedup, states
            ("007 REALTY LLC", 3.0, 300.0, 300.0, 1),
            ("01150 REALTY LLC", 2.0, 200.0, 200.0, 1),
            ("PROGRESS RESIDENTIAL BORROWER 12 LLC", 10.0, 1000.0, 1000.0, 2),
            ("PROGRESS RESIDENTIAL 2015 1 BORROWER", 5.0, 500.0, 400.0, 3),
            ("0", 24.0, 0.0, 0.0, 1),
            ("[UNKNOWN OWNER - source placeholder]", 9.0, 0.0, 0.0, 1),
        ], columns=["owner_norm", "parcels", "total_value", "total_value_dedup", "states"])
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "owner_summary.parquet")
            summary.to_parquet(src, index=False)
            saved = oe.SUMMARY
            oe.SUMMARY = src
            try:
                oe.build_from_summary(buckets=2, outdir=os.path.join(tmp, "out"))
            finally:
                oe.SUMMARY = saved
            con = duckdb.connect()
            out = os.path.join(tmp, "out").replace("\\", "/")
            emap = dict(con.execute(f"select owner_norm, entity_name from "
                                    f"read_parquet('{out}/owner_entity.parquet')").fetchall())
            ent = {r[0]: r[1:] for r in con.execute(
                f"select entity_name, owner_variants, parcels, total_value_dedup, states "
                f"from read_parquet('{out}/owner_entity_summary.parquet')").fetchall()}
            leftovers = [f for f in os.listdir(out) if f.startswith("_es_")]
        self.assertEqual(emap, {"007 REALTY LLC": "007 REALTY", "01150 REALTY LLC": "01150 REALTY",
                                "PROGRESS RESIDENTIAL BORROWER 12 LLC": "PROGRESS RESIDENTIAL",
                                "PROGRESS RESIDENTIAL 2015 1 BORROWER": "PROGRESS RESIDENTIAL"})
        self.assertEqual(ent["PROGRESS RESIDENTIAL"], (2, 15.0, 1400.0, 3))
        self.assertNotIn("REALTY", ent)
        self.assertNotIn("0", ent)
        self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main()
