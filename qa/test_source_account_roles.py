"""Offline source-account regression tests; all writes stay in temporary directories."""
import csv
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import geopandas as gpd
import pandas as pd
from shapely.geometry import box

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import normalize
import resolve
import source_account_roles as roles
import value_overlay


def source_rows():
    # Base quadrant B0/AA/BC must not be mistaken for an interest suffix.
    keys = ["2N3208B000700", "2N3208B000700", "2N3208B000700A1",
            "5N2817AA00700", "5N28110002400", "2N3211BC02100A1"]
    return pd.DataFrame({
        "map_tax": keys, "TLID": keys,
        "account_id": [100006, 100006, 163902, 199999, 164309, 105341],
        "Taxlot": [700, 700, 700, 700, 2400, 2100],
        "S_I_TYPE": [None, None, "A", None, None, "A"],
        "SI_NUMBER": [None, None, 1, None, None, 1],
        "MAILING_NA": ["THE MCGREGOR COMPANY", "THE MCGREGOR COMPANY",
                       "J R ZUKIN CORP", "SYNTHETIC OTHER PARCEL",
                       "PORT OF UMATILLA", "SPECIAL ACCOUNT ONLY"],
        "IN_CARE_OF": [None, None, "DBA MEADOW OUTDOOR ADVERTISING",
                       None, "PNW METAL RECYCLING INC LESSEE", None],
        "AGENT": [None, None, "AGENT CONTACT", None, None, None],
        "RMV_PROPER": [301, 301, 301, 401, 301, 401],
        "PROPERTY_C": [301, 301, 301, 401, 301, 401],
        "SITUS_ADDR": ["4411 NW MCKENNON RD", "4600 NW MCKENNON RD",
                       "4411 NW MCKENNON RD", "OTHER SITE", "82990 BEACH ACCESS RD", None],
    })


class SourceAccountTests(unittest.TestCase):
    def test_full_keys_roles_and_duplicate_situs_survive(self):
        meta = roles.account_fields(source_rows(), "41059")
        self.assertEqual(len(meta), 6)
        self.assertEqual(meta.source_parcel_id.nunique(), 5)
        self.assertEqual(meta.source_base_parcel_id.iloc[0], "2N3208B000700")
        self.assertEqual(meta.source_base_parcel_id.iloc[3], "5N2817AA00700")
        self.assertEqual(meta.source_base_parcel_id.iloc[5], "2N3211BC02100")
        self.assertEqual(meta.source_owner_role.tolist(), ["base_account", "base_account",
                         "special_interest_account", "base_account", "base_account",
                         "special_interest_account"])
        self.assertEqual(meta.source_account_id.iloc[0], "100006")
        self.assertEqual(meta.source_special_interest_number.iloc[2], "1")
        self.assertEqual(meta.source_owner_agent.iloc[2], "AGENT CONTACT")
        self.assertEqual(meta.source_rmv_property_class.iloc[0], "301")

    def test_h_marked_map_prefix_preserves_full_base_and_interest(self):
        raw = source_rows().iloc[[0, 2]].copy()
        raw["map_tax"] = ["1S30H000000100", "1S30H000000100A1"]
        raw["TLID"] = raw["map_tax"]
        meta = roles.account_fields(raw, "41059")
        self.assertEqual(meta.source_base_parcel_id.tolist(), ["1S30H000000100"] * 2)
        self.assertEqual(meta.source_owner_role.tolist(), ["base_account", "special_interest_account"])
        raw["map_tax"] = raw["map_tax"].str.replace("H", "K")
        raw["TLID"] = raw["map_tax"]
        with self.assertRaisesRegex(ValueError, "invalid"):
            roles.account_fields(raw, "41059")

    def test_stale_manifest_overridden_without_mutation(self):
        entry = {"fips": "41059", "owner_fields": "MAILING_NA|IN_CARE_OF|AGENT",
                 "value_field": "RMV_PROPER"}
        fixed = roles.mapping_entry(entry, source_rows())
        self.assertEqual(fixed["owner_fields"], "MAILING_NA")
        self.assertEqual(fixed["value_field"], "NONE")
        self.assertEqual(entry["value_field"], "RMV_PROPER")

    def test_missing_owner_field_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "MAILING_NA"):
            roles.mapping_entry({"fips": "41059"}, source_rows().drop(columns="MAILING_NA"))

    def test_no_full_identity_field_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "bare Taxlot"):
            roles.account_fields(source_rows().drop(columns=["map_tax", "TLID"]), "41059")

    def test_null_full_identity_never_falls_back_to_taxlot(self):
        raw = source_rows()
        raw.loc[0, ["map_tax", "TLID"]] = None
        meta = roles.account_fields(raw, "41059")
        self.assertTrue(pd.isna(meta.source_parcel_id.iloc[0]))
        self.assertEqual(meta.source_owner_role.iloc[0], "unknown")

    def test_tlid_coalesces_and_case_insensitive_columns(self):
        raw = source_rows().drop(columns="map_tax").rename(columns=str.lower)
        meta = roles.account_fields(raw, "41059")
        self.assertEqual(meta.source_parcel_id.iloc[0], "2N3208B000700")
        self.assertEqual(roles.mapping_entry({"fips": "41059"}, raw)["owner_fields"], "mailing_na")

    def test_identity_disagreement_rejected(self):
        raw = source_rows()
        raw.loc[0, "TLID"] = "5N28110002400"
        with self.assertRaisesRegex(ValueError, "disagree"):
            roles.account_fields(raw, "41059")

    def test_invalid_short_or_malformed_key_rejected(self):
        for key in ["700", "2N3208B0", "2N3208B000700A", "2N3208B000700A1junk"]:
            with self.subTest(key=key):
                raw = source_rows()
                raw.loc[0, ["map_tax", "TLID"]] = key
                with self.assertRaisesRegex(ValueError, "invalid"):
                    roles.account_fields(raw, "41059")

    def test_interest_type_and_number_must_match_key(self):
        for field, value in [("S_I_TYPE", "B"), ("SI_NUMBER", 2)]:
            with self.subTest(field=field):
                raw = source_rows()
                raw.loc[2, field] = value
                with self.assertRaisesRegex(ValueError, "disagrees"):
                    roles.account_fields(raw, "41059")

    def test_suffix_role_survives_missing_interest_columns(self):
        meta = roles.account_fields(source_rows().drop(columns=["S_I_TYPE", "SI_NUMBER"]), "41059")
        self.assertEqual(meta.source_owner_role.iloc[2], "special_interest_account")
        self.assertEqual(meta.source_special_interest_type.iloc[2], "A")

    def test_unknown_account_is_not_promoted_to_base(self):
        raw = source_rows()
        raw.loc[0, "account_id"] = None
        self.assertEqual(roles.account_fields(raw, "41059").source_owner_role.iloc[0], "unknown")

    def test_benton_clatsop_preserve_accounts_without_inventing_role(self):
        for fips, attrs, expected in [
            ("41003", {"Account_Num": [166649, 343352], "MapTaxlot": ["12522D000100"] * 2,
                       "In_Care_Of": [None, "CONTACT"]}, ["166649", "343352"]),
            ("41007", {"ACCOUNT_ID": [18529, 64368], "TAXLOTKEY": ["806260000100"] * 2},
             ["18529", "64368"]),
        ]:
            with self.subTest(fips=fips):
                meta = roles.account_fields(pd.DataFrame(attrs), fips)
                self.assertEqual(meta.source_account_id.tolist(), expected)
                self.assertEqual(meta.source_owner_role.tolist(), ["unknown", "unknown"])

    def test_other_county_mappings_unchanged(self):
        entry = {"fips": "53033", "owner_fields": "OWNER", "value_field": "VALUE"}
        self.assertEqual(roles.mapping_entry(entry, source_rows()), entry)
        self.assertTrue(roles.account_fields(source_rows(), "53033").isna().all().all())

    def test_crosswalk_and_live_value_sentinel(self):
        with (ROOT / "src/county_methods.csv").open(encoding="utf-8") as stream:
            row = next(r for r in csv.DictReader(stream) if r["fips"] == "41059")
        self.assertEqual(row["owner_fields"], "MAILING_NA")
        self.assertEqual(row["value_field"], "NONE")
        self.assertIsNone(resolve._best_value({"RMV_PROPER": 301, "total": 900}, "NONE"))
        self.assertEqual(resolve._best_value({"total": 900}, "total"), ("total", 900.0))


class NormalizationIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="npc_source_accounts_")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.overlay_patch = patch.object(value_overlay, "OVERLAY", str(self.root / "overlay"))
        self.overlay_patch.start()
        self.addCleanup(self.overlay_patch.stop)
        self.window_patch = patch.object(normalize, "_state_window", return_value=[-125, 41, -116, 47])
        self.window_patch.start()
        self.addCleanup(self.window_patch.stop)
        self.raw = self.root / "41059_umatilla.parquet"
        self.out_dir = self.root / "normalized"
        self.gdf = gpd.GeoDataFrame(source_rows(), geometry=[box(-119, 45, -118.999, 45.001)] * 6,
                                   crs="EPSG:4326")
        self.gdf.to_parquet(self.raw)
        self.entry = {"fips": "41059", "file": str(self.raw), "state": "OR", "county": "Umatilla",
                      "owner_fields": "MAILING_NA|IN_CARE_OF|AGENT", "value_field": "RMV_PROPER",
                      "source_url": "https://example.invalid/offline-fixture", "retrieved_at": "2026-09-12"}

    def test_normalize_roundtrip_separates_roles_and_blocks_class_dollars(self):
        _, fmap, _ = normalize.normalize_county(self.entry, str(self.out_dir))
        result = gpd.read_parquet(self.out_dir / self.raw.name)
        self.assertEqual(result.columns.tolist(), normalize.SCHEMA)
        self.assertEqual(len(result), 6)
        self.assertEqual(result.apn.tolist(), self.gdf.map_tax.tolist())
        self.assertEqual(result.owner.iloc[4], "PORT OF UMATILLA")
        self.assertEqual(result.source_owner_in_care_of.iloc[4], "PNW METAL RECYCLING INC LESSEE")
        self.assertTrue(result.assessed_value.isna().all())
        self.assertEqual(str(result.assessed_value.dtype), "Int64")
        self.assertEqual(result.source_rmv_property_class.iloc[0], "301")
        self.assertIn("validated full key", fmap["apn"])
        self.assertTrue(result.geometry.equals(self.gdf.geometry))
        normalize.normalize_county(self.entry, str(self.out_dir))
        pd.testing.assert_frame_equal(result, gpd.read_parquet(self.out_dir / self.raw.name))

    def test_null_owner_not_filled_from_agent_or_care_of(self):
        self.gdf.loc[4, "MAILING_NA"] = None
        self.gdf.to_parquet(self.raw)
        normalize.normalize_county(self.entry, str(self.out_dir))
        result = gpd.read_parquet(self.out_dir / self.raw.name)
        self.assertTrue(pd.isna(result.owner.iloc[4]))
        self.assertTrue(pd.notna(result.source_owner_in_care_of.iloc[4]))

    def test_registered_overlay_fails_before_replacing_output(self):
        normalize.normalize_county(self.entry, str(self.out_dir))
        target = self.out_dir / self.raw.name
        before = target.read_bytes()
        overlay = Path(value_overlay.overlay_path("41059"))
        overlay.parent.mkdir()
        pd.DataFrame({"apn": ["700"], "assessed_value": [301]}).to_parquet(overlay)
        overlay_before = overlay.read_bytes()
        with self.assertRaisesRegex(ValueError, "quarantined"):
            normalize.normalize_county(self.entry, str(self.out_dir))
        with patch.object(value_overlay, "NORM", str(self.out_dir)):
            with self.assertRaisesRegex(ValueError, "quarantined"):
                value_overlay.apply_standalone("41059")
            with self.assertRaisesRegex(ValueError, "quarantined"):
                value_overlay.capture("41059")
        self.assertEqual(target.read_bytes(), before)
        self.assertEqual(overlay.read_bytes(), overlay_before)

    def test_capture_blocked_even_without_old_overlay(self):
        with self.assertRaisesRegex(ValueError, "quarantined"):
            value_overlay.capture("41059")
        self.assertFalse(Path(value_overlay.OVERLAY).exists())


if __name__ == "__main__":
    unittest.main()
