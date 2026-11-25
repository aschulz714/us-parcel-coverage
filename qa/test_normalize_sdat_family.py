"""Offline regression tests for the 2026-09-14 degenerate-APN / MD SDAT-family field mapping.

Builds tiny synthetic raw parquets with the real source column names and runs
normalize.normalize_county() into a temp dir (no canonical data, no network).
"""
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import geopandas as gpd
import pandas as pd
import pyarrow.parquet as pq
from shapely.geometry import box

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))
os.chdir(SRC)
import normalize  # noqa: E402

N = 60


def _geoms(lon0, lat0):
    return [box(lon0 + i * 0.001, lat0, lon0 + i * 0.001 + 0.0008, lat0 + 0.0008) for i in range(N)]


class SdatFamilyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sdat-test-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, fips, state, county, columns, lon0, lat0, value_field=None, owner_fields=""):
        gdf = gpd.GeoDataFrame(columns, geometry=_geoms(lon0, lat0), crs=4326)
        raw = self.tmp / f"{fips}_{county.lower()}.parquet"
        gdf.to_parquet(raw)
        entry = {"fips": fips, "state": state, "county": county, "file": str(raw),
                 "owner_fields": owner_fields, "value_field": value_field,
                 "source_url": "https://example.test/arcgis/rest/services/x/FeatureServer/0/query",
                 "retrieved_at": "2026-09-14T00:00:00Z", "source_vintage": "", "source_last_edit": "",
                 "method": "arcgis", "feature_count": N}
        out_dir = self.tmp / "out"
        norm_rel, fmap, populated = normalize.normalize_county(entry, str(out_dir))
        out = pq.read_table(str(out_dir / raw.name)).to_pandas()
        return fmap, out

    def test_md_imap_sdat_schema(self):
        cols = {
            "OBJECTID": range(1, N + 1),
            "ACCTID": [f"0512{i:06d}" for i in range(N)],
            "PARCEL": ["0012"] * N,                       # grid number: the old (wrong) pick
            "MAP": ["0034"] * N, "GRID": ["0003"] * N,
            "ADDRESS": [f"{100 + i} MAIN ST" for i in range(N)],
            "OWNADD1": [f"{200 + i} ELM AVE" for i in range(N)],
            "OWNCITY": ["COLUMBIA"] * N, "OWNSTATE": ["MD"] * N, "OWNERZIP": ["21044"] * N,
            "SQFTSTRC": [1200 + i for i in range(N)],
            "CONSIDR1": [250000 + i * 1000 for i in range(N)],
            "TRADATE": [1_600_000_000_000 + i * 86_400_000 for i in range(N)],  # ms epoch
            "NFMLNDVL": [90000 + i for i in range(N)],
            "NFMIMPVL": [210000 + i for i in range(N)],
            "NFMTTLVL": [300000 + 2 * i for i in range(N)],
            "LU": ["R"] * N, "DESCLU": ["Residential"] * N, "ACRES": [0.25] * N, "YEARBLT": [1990] * N,
        }
        fmap, out = self._run("24027", "MD", "Howard", cols, -76.9, 39.2, value_field="NFMTTLVL")
        self.assertEqual(fmap["apn"], "ACCTID")
        self.assertEqual(out["apn"].nunique(), N)
        self.assertEqual(fmap["mailing_address"], "OWNADD1")
        self.assertEqual(fmap["building_sqft"], "SQFTSTRC")
        self.assertEqual(fmap["sale_price"], "CONSIDR1")
        self.assertEqual(fmap["sale_date"], "TRADATE")
        self.assertEqual(fmap["land_value"], "NFMLNDVL")
        self.assertEqual(fmap["improvement_value"], "NFMIMPVL")
        self.assertEqual(fmap["assessed_value"], "NFMTTLVL")
        self.assertEqual(int(out["building_sqft"].iloc[0]), 1200)
        self.assertEqual(int(out["sale_price"].iloc[0]), 250000)
        self.assertEqual(int(out["land_value"].iloc[0]), 90000)
        self.assertEqual(int(out["improvement_value"].iloc[0]), 210000)
        self.assertEqual(out["sale_date"].iloc[0], "2020-09-13")
        self.assertTrue(out["mailing_address"].notna().all())

    def test_harford_schema(self):
        cols = {
            "OBJECTID": range(1, N + 1),
            "FEATURE": [f"01{i:06d}" for i in range(N)],
            "PARCEL": ["0557"] * N, "MAP": ["0065"] * N,
            "OWN_1": ["SMITH JOHN"] * N,
            "OWN_AD_1": [f"{300 + i} OAK RD" for i in range(N)],
            "OWN_CITY": ["BEL AIR"] * N, "OWN_STATE": ["MD"] * N,
            "CON_AMT": [180000.0 + i for i in range(N)],
            "CUR_IMP_VA": [150000.0 + i for i in range(N)],
            "CUR_T_ASSM": [260000.0 + i for i in range(N)],
            "TRANS_DATE": [1_600_000_000_000] * N,
            "ST_SQ_FT": [1400.0] * N, "YR_BUILT": ["1967"] * N, "LANDUSE": ["R"] * N,
            "Shape_Acre": [0.5] * N,
        }
        fmap, out = self._run("24025", "MD", "Harford", cols, -76.3, 39.5,
                              value_field="CUR_T_ASSM", owner_fields="OWN_1")
        self.assertEqual(fmap["apn"], "FEATURE")
        self.assertEqual(out["apn"].nunique(), N)
        self.assertEqual(fmap["mailing_address"], "OWN_AD_1")
        self.assertEqual(fmap["sale_price"], "CON_AMT")
        self.assertEqual(fmap["improvement_value"], "CUR_IMP_VA")
        self.assertEqual(fmap["building_sqft"], "ST_SQ_FT")
        self.assertEqual(fmap["assessed_value"], "CUR_T_ASSM")

    def test_washington_schema_with_mmddyyyy_dates(self):
        cols = {
            "OBJECTID": range(1, N + 1),
            "TaxAccNo": [f"01{i:06d}" for i in range(N)],
            "AcctNo": [f"{i:06d}" for i in range(N)],
            "Parcel": ["0254"] * N, "Map": ["0080"] * N,
            "OwnName1": ["DOE JANE"] * N,
            "OwnAdd1": [f"{400 + i} MILLS RD" for i in range(N)],
            "OwnCity": ["HAGERSTOWN"] * N, "OwnState": ["MD"] * N, "OwnZip": ["21740"] * N,
            "StructArea": [1800 + i for i in range(N)],
            "SS1Consid": [200000 + i for i in range(N)],
            "SS1TransDate": ["10162013"] * N,
            "CurLandVal": [60000 + i for i in range(N)],
            "CurImpVal": [190000 + i for i in range(N)],
            "CYAssess": [250000 + 2 * i for i in range(N)],
            "LandUseCode": ["R"] * N, "YrBuilt": [1975] * N, "CalcAcre": [0.2] * N,
        }
        fmap, out = self._run("24043", "MD", "Washington", cols, -77.7, 39.6,
                              value_field="CYAssess", owner_fields="OwnName1")
        self.assertEqual(fmap["apn"], "TaxAccNo")
        self.assertEqual(out["apn"].nunique(), N)
        self.assertEqual(fmap["mailing_address"], "OwnAdd1")
        self.assertEqual(fmap["building_sqft"], "StructArea")
        self.assertEqual(fmap["sale_price"], "SS1Consid")
        self.assertEqual(fmap["improvement_value"], "CurImpVal")
        self.assertEqual(fmap["land_value"], "CurLandVal")
        self.assertEqual(fmap["assessed_value"], "CYAssess")
        self.assertEqual(fmap["sale_date"], "SS1TransDate")
        self.assertEqual(out["sale_date"].iloc[0], "2013-10-16")

    def test_candler_parcellabel(self):
        cols = {
            "OBJECTID": range(1, N + 1),
            "NAME": [f"00{i % 9 + 1}    {i:03d}" for i in range(N)],
            "PARCELLABEL": [f"00{i % 9 + 1}    {i:03d}" for i in range(N)],
            "PARCEL": [str(i % 7 + 1) for i in range(N)],  # bare component: 7 distinct
            "MAP": ["001"] * N, "BLOCK": [""] * N,
        }
        fmap, out = self._run("13043", "GA", "Candler County", cols, -82.1, 32.4)
        self.assertEqual(fmap["apn"], "PARCELLABEL")
        self.assertEqual(out["apn"].nunique(), N)

    def test_unrelated_county_unchanged_by_new_patterns(self):
        # A source with only the classic names must still map exactly as before.
        cols = {
            "OBJECTID": range(1, N + 1),
            "PARCEL_ID": [f"P{i:05d}" for i in range(N)],
            "OWNER_ADDR": [f"{i} PINE ST" for i in range(N)],
            "SALE_PRICE": [100000 + i for i in range(N)],
            "SALE_DATE": ["2021-01-0" + str(i % 9 + 1) for i in range(N)],
            "LAND_VAL": [50000] * N, "IMP_VAL": [50000] * N, "TOTAL_VAL": [100000] * N,
            "BLDG_SQFT": [900] * N,
        }
        fmap, out = self._run("53043", "WA", "Lincoln", cols, -118.4, 47.6, value_field="TOTAL_VAL")
        self.assertEqual(fmap["apn"], "PARCEL_ID")
        self.assertEqual(fmap["mailing_address"], "OWNER_ADDR")
        self.assertEqual(fmap["sale_price"], "SALE_PRICE")
        self.assertEqual(fmap["sale_date"], "SALE_DATE")
        self.assertEqual(fmap["land_value"], "LAND_VAL")
        self.assertEqual(fmap["improvement_value"], "IMP_VAL")
        self.assertEqual(fmap["building_sqft"], "BLDG_SQFT")


if __name__ == "__main__":
    unittest.main(verbosity=1)
