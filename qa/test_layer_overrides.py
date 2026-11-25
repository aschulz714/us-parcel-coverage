"""Offline tests for normalize.LAYER_FIELD_OVERRIDES (source fan-out step 3, 2026-09-28):
statewide-layer field names that defeat the patterns are mapped explicitly, only for that layer."""
import os
import sys
import tempfile
import unittest

import geopandas as gpd
import pandas as pd
from shapely.geometry import box

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
import normalize  # noqa: E402
import value_overlay  # noqa: E402


def _raw(tmp, fips, cols, lon, lat):
    n = len(next(iter(cols.values())))
    gdf = gpd.GeoDataFrame(cols, geometry=[box(lon + i * 0.001, lat, lon + i * 0.001 + 0.0008, lat + 0.0008)
                                           for i in range(n)], crs="EPSG:4326")
    path = os.path.join(tmp, f"{fips}_synth.parquet")
    gdf.to_parquet(path)
    return path


def _run(tmp, fips, state, cols, url, lon, lat, owner="OWNER"):
    value_overlay.OVERLAY = os.path.join(tmp, "value_overlay")
    os.makedirs(value_overlay.OVERLAY, exist_ok=True)
    raw = _raw(tmp, fips, cols, lon, lat)
    entry = {"file": raw, "fips": fips, "state": state, "county": "Synth", "owner_fields": owner,
             "value_field": None, "source_url": url, "retrieved_at": "2026-09-28", "source_last_edit": None}
    out_dir = os.path.join(tmp, "normalized")
    _, fmap, _ = normalize.normalize_county(entry, out_dir)
    return fmap, gpd.read_parquet(os.path.join(out_dir, os.path.basename(raw))).set_index("apn")


CO_COLS = {"parcel_id": ["A1", "A2", "A3"], "owner": ["X LLC", "Y TRUST", "Z INC"],
           "situsAdd": ["1 N MAIN ST", "2 N MAIN ST", "3 N MAIN ST"],
           "ownerAdd": ["PO BOX 9", "10 ELM ST", "11 OAK AVE"], "ownAddCty": ["DENVER", "BOULDER", "AURORA"],
           "ownAddStt": ["CO", "CO", "TX"], "ownAddZip": ["80206-2635", "80302", "75001"],
           "landUseDsc": ["RESIDENTIAL", "COMMERCIAL", "VACANT"], "apprValTot": [100000, 200000, 300000]}


class LayerOverrides(unittest.TestCase):
    def test_colorado_owner_mailing_block_is_mapped(self):
        with tempfile.TemporaryDirectory() as tmp:
            fmap, out = _run(tmp, "08999", "CO", CO_COLS,
                             "https://gis.colorado.gov/public/rest/services/Address_and_Parcel/"
                             "Colorado_Public_Parcels/FeatureServer/0/query", -105.0, 39.7, owner="owner")
            self.assertEqual(fmap["mailing_address"], "ownerAdd")
            self.assertEqual((fmap["mail_city"], fmap["mail_zip"], fmap["land_use_desc"]),
                             ("ownAddCty", "ownAddZip", "landUseDsc"))
            self.assertEqual(out.loc["A2", "mailing_address"], "10 ELM ST")
            self.assertEqual(out.loc["A3", "mail_state"], "TX")
            self.assertEqual(out.loc["A1", "mail_city"], "DENVER")

    def test_same_fields_on_another_layer_are_not_forced(self):
        with tempfile.TemporaryDirectory() as tmp:
            fmap, _ = _run(tmp, "08999", "CO", CO_COLS, "https://example.org/other/FeatureServer/0/query",
                           -105.0, 39.7, owner="owner")
            self.assertNotEqual(fmap["mailing_address"], "ownerAdd")  # no generic 'ownerAdd' pattern

    def test_wisconsin_postal_string_gives_state_and_zip_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            cols = {"PARCELID": ["W1", "W2", "W3"], "OWNERNME1": ["A", "B", "C"],
                    "SITEADRESS": ["9572 OVERLAND RD", None, "12 PINE ST"],
                    "PSTLADRESS": ["9601 OVERLAND RD, MOUNT HOREB WI  53572",
                                   "PO BOX 1450 CHICAGO, IL 60690", "260 W SEEBOTH ST MILWAUKEE, WI 53204-1234"]}
            fmap, out = _run(tmp, "55999", "WI", cols,
                             "https://services3.arcgis.com/x/arcgis/rest/services/"
                             "Wisconsin_Statewide_Parcels_DB/FeatureServer/0/query", -89.6, 43.0, owner="OWNERNME1")
            self.assertEqual(fmap["situs_address"], "SITEADRESS")
            self.assertEqual(out.loc["W1", "situs_address"], "9572 OVERLAND RD")
            self.assertEqual(list(out["mail_state"]), ["WI", "IL", "WI"])
            self.assertEqual(list(out["mail_zip"]), ["53572", "60690", "53204"])
            self.assertIsNone(fmap["mailing_address"])  # street can't be split from the city reliably

    def test_mn_address_block_layouts(self):
        # real layouts sampled 2026-09-28 from MnGeo plan_parcels_open
        rows = [  # (l1, l2, l3, l4) -> expected (street, city, state, zip)
            (("7038 Ambassador Blvd Nw", "St. Francis", "Mn", "55070"),
             ("7038 Ambassador Blvd Nw", "St. Francis", "MN", "55070")),                      # Anoka split
            (("1128 Harmon Pl Ste 230", None, "Minneapolis, MN 55403", None),
             ("1128 Harmon Pl Ste 230", "Minneapolis", "MN", "55403")),                       # Dakota
            (("1624 Rome Ave", "Saint Paul, MN 55116", None, None),
             ("1624 Rome Ave", "Saint Paul", "MN", "55116")),                                 # Crow Wing
            (("Aitkin County Treasurer", "307 Second St Nw Rm 119", "Aitkin, MN 56431", None),
             ("307 Second St Nw Rm 119", "Aitkin", "MN", "56431")),                           # name on l1
            (("177 B-Brook Knoll Dr", "Santa Cruz, CA 95060-1234", None, None),
             ("177 B-Brook Knoll Dr", "Santa Cruz", "CA", "95060")),                          # out of state
            (("Saint Paul, MN 55155", None, None, None), (None, None, None, None)),             # no street
        ]
        gdf = pd.DataFrame([r[0] for r in rows], columns=["l1", "l2", "l3", "l4"])
        street, city, st, zp = normalize._address_block(gdf, ["l1", "l2", "l3", "l4"])
        got = [tuple(None if pd.isna(x) else x for x in t) for t in zip(street, city, st, zp)]
        self.assertEqual(got, [r[1] for r in rows])

    def test_empty_override_column_falls_back_to_patterns(self):
        cols = ["ownerAdd", "MAIL_ADDR1"]
        gdf = pd.DataFrame({"ownerAdd": [None] * 100, "MAIL_ADDR1": ["1 A ST"] * 100})
        self.assertEqual(normalize._layer_overrides("x/Colorado_Public_Parcels/y", cols, gdf), {})


if __name__ == "__main__":
    unittest.main()
