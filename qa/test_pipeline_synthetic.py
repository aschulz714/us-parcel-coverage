#!/usr/bin/env python3
"""Synthetic-county integration test for the normalize chain.

Runs a tiny fake county (10 rows, fips 56999 'Synthville' WY) through
normalize_county -> value_overlay -> value_clean and asserts none of the
historical silent-data-loss bug classes regress:

  1. comma/'$'-formatted value strings coerce to dollars, not NULL   (43045cd, ~6.7M lost)
  2. a DECLARED value_field named like a tax column is trusted        (07f7f49, Lake IL 278k)
  3. value overlay fills NULL value and survives a re-normalize       (aff6698, 4.7M wiped)
  4. all-9s placeholder values are nulled by value_clean              (29713cd, $77B artifacts)
  5. displaced geometry: junk rows dropped, real rows kept geom-nulled
  6. sqft-stored acreage repaired via polygon-area arbitration        (Hamilton TN class)
  7. owner placeholder strings rewritten to bracketed sentinels
  8. mail_state parsed from a city/state/zip column
  9. output schema == normalize.SCHEMA, assessed_value dtype Int64
 10. re-normalize is idempotent (same output twice, overlay intact)

Entirely self-contained: writes to a temp dir, never touches data/parcels or the
manifest. Exit 0 = pass, 1 = fail.

  python qa/test_pipeline_synthetic.py
"""
import os
import shutil
import sys
import tempfile

import geopandas as gpd
import pandas as pd
from shapely.geometry import box

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import normalize
import value_clean
import value_overlay

FIPS = "56999"  # WY prefix (real state window), fake county — never collides with real data


def build_raw(tmp):
    """10-row synthetic county exercising every trap. Lon/lat inside the WY window."""
    sq = lambda x, y, d=0.001: box(x, y, x + d, y + d)
    # ~0.5-acre polygon (45m x 45m at 43N) for the sqft-acreage repair row
    half_acre = box(-105.2, 43.2, -105.2 + 0.000553, 43.2 + 0.000406)
    rows = [
        # apn    owner             value          situs          csz                  acres  geom
        ("0001", "ALPHA RANCH LLC", "99,400",     "1 MAIN ST",   None,                1.5,   sq(-105.00, 43.00)),
        ("0002", "BETA CORP",       "$123,456",   "2 MAIN ST",   None,                2.0,   sq(-105.01, 43.01)),
        ("0003", "NOT PUBLISHED",   " 250000 ",   "3 MAIN ST",   None,                0.8,   sq(-105.02, 43.02)),
        ("0004", "GAMMA TRUST",     None,         "4 MAIN ST",   None,                1.0,   sq(-105.03, 43.03)),
        ("0005", "DELTA FARMS",     "9999999",    "5 MAIN ST",   None,                3.0,   sq(-105.04, 43.04)),
        ("0006", "EPSILON LP",      "300000",     "6 MAIN ST",   None,                1.2,   sq(0.0, 0.0)),        # displaced, real row
        (None,   None,              None,         None,          None,                None,  sq(0.5, 0.5)),        # displaced, pure junk
        ("0008", "ZETA INC",        "42",         "8 MAIN ST",   None,                21780, half_acre),           # sqft in acres col
        ("0009", "ETA LLC",         "150,000",    "9 MAIN ST",   "CHEYENNE WY 82001", 0.5,   sq(-105.05, 43.05)),
        ("0010", "THETA CO",        "0",          "10 MAIN ST",  None,                1.1,   sq(-105.06, 43.06)),
    ]
    gdf = gpd.GeoDataFrame(
        {"PARCEL_NUMBER": [r[0] for r in rows],
         "OWNER_NAME": [r[1] for r in rows],
         "ASSESS_TOTAL_TAXYR": [r[2] for r in rows],   # name contains 'total_tax' — declared-field trust test
         "SITUS_ADDR": [r[3] for r in rows],
         "CITY_ST_ZIP": [r[4] for r in rows],
         "MAIL_ADDR1": ["PO BOX 1" if r[0] else None for r in rows],
         "LAND_USE": ["RESIDENTIAL" if r[0] else None for r in rows],
         "ACRES": [r[5] for r in rows]},
        geometry=[r[6] for r in rows], crs="EPSG:4326")
    raw = os.path.join(tmp, f"{FIPS}_synthville.parquet")
    gdf.to_parquet(raw)
    return raw


def run():
    tmp = tempfile.mkdtemp(prefix="npc_synth_")
    fails = []
    # NA-safe: pd.NA comparisons raise on bool(); treat NA/exception as failure, not a crash
    def ok(cond, msg):
        try:
            good = bool(cond) and not pd.isna(cond)
        except (TypeError, ValueError):
            good = False
        fails.append(msg) if not good else print(f"  ok   {msg}")
    eq = lambda a, b: pd.notna(a) and a == b
    try:
        raw = build_raw(tmp)
        out_dir = os.path.join(tmp, "normalized")

        # synthetic overlay: apn 0004 (NULL in raw) -> 777000; redirect the overlay store to tmp
        overlay_dir = os.path.join(tmp, "value_overlay")
        os.makedirs(overlay_dir)
        value_overlay.OVERLAY = overlay_dir
        pd.DataFrame({"apn": ["0004"], "assessed_value": [777000],
                      "value_source": ["synthetic-test"]}).to_parquet(
            value_overlay.overlay_path(FIPS), index=False)

        entry = {"file": raw, "fips": FIPS, "state": "WY", "county": "Synthville",
                 "owner_fields": "OWNER_NAME", "value_field": "ASSESS_TOTAL_TAXYR",
                 "source_url": "synthetic://test", "retrieved_at": "2026-07-01",
                 "source_last_edit": 2026}
        _, fmap, _ = normalize.normalize_county(entry, out_dir)
        out_path = os.path.join(out_dir, os.path.basename(raw))
        out = gpd.read_parquet(out_path)
        v = out.set_index("apn")["assessed_value"]

        ok(list(out.columns) == normalize.SCHEMA, "output columns == normalize.SCHEMA")
        ok(str(out["assessed_value"].dtype) == "Int64", "assessed_value dtype is Int64")
        ok(len(out) == 9, f"junk displaced row dropped (9 rows, got {len(out)})")
        ok(fmap["assessed_value"] == "ASSESS_TOTAL_TAXYR",
           "declared tax-named value_field trusted (Lake IL class)")
        ok(eq(v.get("0001"), 99400) and eq(v.get("0009"), 150000),
           "comma-formatted values coerced (99,400 / 150,000)")
        ok(eq(v.get("0002"), 123456), "'$'-prefixed value coerced ($123,456)")
        ok(eq(v.get("0003"), 250000), "whitespace-padded value coerced")
        ok(eq(v.get("0010"), 0), "genuine $0 preserved as 0, not NULL")
        ok(eq(v.get("0004"), 777000), "overlay filled NULL assessed_value (durable backfill)")
        ok(out.set_index("apn")["owner"].get("0003", "").startswith("["),
           "placeholder owner rewritten to bracketed sentinel")
        r6 = out[out["apn"] == "0006"]
        ok(len(r6) == 1 and r6.geometry.isna().all(),
           "displaced real row kept with geometry nulled")
        ok(abs(float(out.set_index("apn")["acreage"].get("0008", 0)) - 0.5) < 0.01,
           "sqft-stored acreage repaired to 0.5 ac (polygon-arbitrated)")
        ok(eq(out.set_index("apn")["mail_state"].get("0009"), "WY"),
           "mail_state parsed from city/state/zip")

        # stage 2: value_clean nulls the all-9s placeholder, touches nothing else
        value_clean.clean_file(out_path)
        v2 = gpd.read_parquet(out_path).set_index("apn")["assessed_value"]
        ok(pd.isna(v2.get("0005")), "all-9s placeholder (9999999) nulled by value_clean")
        ok(eq(v2.get("0001"), 99400) and eq(v2.get("0004"), 777000),
           "value_clean left real values + overlay value intact")

        # stage 3: re-normalize (simulated re-pull) — output identical, overlay survives
        normalize.normalize_county(entry, out_dir)
        v3 = gpd.read_parquet(out_path).set_index("apn")["assessed_value"]
        ok(eq(v3.get("0004"), 777000), "overlay value SURVIVES re-normalize (aff6698 class)")
        ok(len(v3) == 9 and eq(v3.get("0001"), 99400), "re-normalize idempotent")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if fails:
        for m in fails:
            print(f"  FAIL {m}")
        print(f"SYNTHETIC PIPELINE TEST: {len(fails)} FAILURE(S)")
        return 1
    print("SYNTHETIC PIPELINE TEST: ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(run())
