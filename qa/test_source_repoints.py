"""Offline tests for the 2026-09-28 source repoints (recon/source_fanout_2026-09-27.md):
statewide slicers for the moved WI/MT layers, the O'Brien IA name quirk, the bulk-download
HTML guard, and a crosswalk guard against routing to hosts known dead."""
import csv
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import ingest  # noqa: E402
import ingest_bulk_wa  # noqa: E402

WI_URL = ("https://services3.arcgis.com/n6uYoouQZW75n5WI/arcgis/rest/services/"
          "Wisconsin_Statewide_Parcels_DB/FeatureServer/0/query")
MT_URL = "https://gisservice.mt.gov/arcgis/rest/services/msdi_cadastral_map_v1/MapServer/1/query"
IA_URL = ("https://services3.arcgis.com/kd9gaiUExYqUbnoq/arcgis/rest/services/"
          "Iowa_Parcels_2017/FeatureServer/0/query")

# Hosts/layers verified dead 2026-09-27/28; no crosswalk row may route to them.
DEAD_ROUTES = (
    "gisservicemt.gov/arcgis/rest/services/MSDI_Framework/Parcels",
    "Wisconsin_Statewide_Parcels/FeatureServer",
    "Malheur_County_Web_Map_3_WFL1",
    "GCGIS_BaseMap/MapServer/2",
)


class StatewideSlicers(unittest.TestCase):
    def test_wi_successor_keeps_parcelfips_slice(self):
        row = {"query_url": WI_URL, "fips": "55025", "county": "Dane"}
        self.assertEqual(ingest._statewide_where(row), "PARCELFIPS='025'")

    def test_mt_successor_slices_by_county_name(self):
        row = {"query_url": MT_URL, "fips": "30049", "county": "Lewis and Clark"}
        self.assertEqual(ingest._statewide_where(row), "UPPER(CountyName)='LEWIS AND CLARK'")

    def test_obrien_uses_the_layers_spaced_spelling(self):
        row = {"query_url": IA_URL, "fips": "19141", "county": "O'Brien"}
        self.assertEqual(ingest._statewide_where(row), "COUNTYNAME='O'' BRIEN'")

    def test_other_iowa_counties_unchanged(self):
        row = {"query_url": IA_URL, "fips": "19001", "county": "Adair"}
        self.assertEqual(ingest._statewide_where(row), "COUNTYNAME='ADAIR'")


class BulkDownloadGuard(unittest.TestCase):
    def test_html_error_page_is_rejected(self):
        page = b"<!DOCTYPE html><html><head><title>404 Page Not Found</title></head></html>"
        with self.assertRaisesRegex(RuntimeError, "not a zip"):
            ingest_bulk_wa.check_download("gh_parcel_gdb.zip", "https://x/Parcel.gdb.zip",
                                          page, "text/html; charset=UTF-8")

    def test_zip_body_is_accepted(self):
        ingest_bulk_wa.check_download("gh_parcel_gdb.zip", "https://x/Parcel.gdb.zip",
                                      b"PK\x03\x04" + b"\x00" * 64, "application/zip")

    def test_rejected_download_keeps_cached_file(self):
        class FakeResponse:
            content = b"<html>moved</html>"
            headers = {"Content-Type": "text/html"}

            def raise_for_status(self):
                pass

        with tempfile.TemporaryDirectory() as tmp:
            cached = os.path.join(tmp, "gh_parcel_gdb.zip")
            with open(cached, "wb") as f:
                f.write(b"PK\x03\x04good")
            import requests
            orig_get, orig_bulk = requests.get, ingest_bulk_wa.BULK
            orig_sources = ingest_bulk_wa.BULK_SOURCES
            requests.get = lambda *a, **k: FakeResponse()
            ingest_bulk_wa.BULK = tmp
            ingest_bulk_wa.BULK_SOURCES = {"grays_harbor": {"gh_parcel_gdb.zip": "https://x"}}
            try:
                with self.assertRaises(RuntimeError):
                    ingest_bulk_wa.download_sources("grays_harbor", force=True)
            finally:
                requests.get, ingest_bulk_wa.BULK = orig_get, orig_bulk
                ingest_bulk_wa.BULK_SOURCES = orig_sources
            with open(cached, "rb") as f:
                self.assertEqual(f.read(), b"PK\x03\x04good")


class CrosswalkRoutes(unittest.TestCase):
    def test_no_row_routes_to_a_known_dead_layer(self):
        with open(ROOT / "src" / "county_methods.csv", encoding="utf-8") as f:
            bad = [(r["fips"], r["query_url"]) for r in csv.DictReader(f)
                   if any(d in (r["query_url"] or "") for d in DEAD_ROUTES)]
        self.assertEqual(bad, [])


if __name__ == "__main__":
    unittest.main()
