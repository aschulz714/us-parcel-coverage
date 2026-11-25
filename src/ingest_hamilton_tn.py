"""Hamilton TN 47065 (Chattanooga) — free assessor bulk extract, ATTRIBUTES ONLY.

FALLBACK ONLY (2026-06-12): the PRIMARY route is now the county's own ArcGIS layer
(mapsdev.hamiltontn.gov Live_Parcels — full owners + values + LUCODE + polygon
geometry, routed in county_methods.csv via plain ingest.py). Keep this script in
case that dev-host REST server vanishes.

Hamilton is one of the 5 TN web-app-only counties absent from both the TN statewide
layer and the Comptroller parcel program; county GIS sells geometry for $2,500. But
the assessor publishes the full roll free, refreshed ~monthly (verified 2026-06-12):
  https://www.hamiltontn.gov/DownloadRecords.aspx -> AssessorExport.zip (fixed-width)
(The CSV variant AssessorExportCSV.zip is currently a header-only stub on the server.)

KNOWN QUIRKS if this fallback is ever used: (1) values are CENTS (2 implied decimals)
and acres have 4 implied decimals — handled below; (2) CalcAcres MIXES UNITS row-by-row
(some rows are square feet: '21780.0000' = exactly 0.5 ac; DeedAcres/MapAcres are always
0 so there is no in-file arbiter) — the REST layer's CALCACRES does not have this defect;
(3) ~3 records contain embedded newlines and arrive as split/garbled line pairs.

Fixed-width positions come from the county's official AssessorExtractLayout.pdf
(record length 1999, lines right-trimmed; layout cached beside the zip). Geometry is
NULL on every row — parcels are queryable via research.py but won't render on the map.
Unlike the rest of TN (statewide layer publishes no values), this roll carries full
land/building/appraised values.

Usage: python src/ingest_hamilton_tn.py   (expects the zip already downloaded, or fetches it)
"""
import datetime, io, os, sys, zipfile

import geopandas as gpd
import pandas as pd
import requests

sys.path.insert(0, os.path.dirname(__file__))
import ingest

HERE = os.path.dirname(__file__)
ROOT = os.path.dirname(HERE)
SRC_DIR = os.path.join(ROOT, "data", "parcels", "bulk_src", "hamilton_tn")
ZIP_PATH = os.path.join(SRC_DIR, "AssessorExport.zip")
URL = "https://www.hamiltontn.gov/_downloadsAssessor/AssessorExport.zip"
RAW = os.path.join(ROOT, "data", "parcels", "raw")

# (name, start, end) — 1-based inclusive positions from AssessorExtractLayout.pdf
LAYOUT = [
    ("GISLink", 1, 16),
    ("OwnerName1", 17, 166),
    ("OwnerName2", 167, 316),
    ("PropAddress", 537, 596),     # county-composed situs line
    ("Map", 597, 608),
    ("Group", 609, 620),
    ("Parcel", 621, 632),
    ("TaxMapNumber", 633, 647),
    ("MailStNum", 648, 652),
    ("MailStDirPfx", 653, 655),
    ("MailStName", 656, 695),
    ("MailTypeSfx", 696, 700),
    ("MailUnit", 701, 710),
    ("MailAddrLine2", 711, 750),
    ("MailCity", 751, 775),
    ("MailState", 776, 785),
    ("MailZip", 786, 801),
    ("CalcAcres", 1127, 1146),
    ("LUCode", 1187, 1205),
    ("LandValue", 1218, 1237),
    ("BuildingValue", 1238, 1257),
    ("AppraisedValue", 1258, 1277),
    ("AssessedValue", 1278, 1297),
    ("PropType", 1330, 1331),
]


def main():
    if not os.path.exists(ZIP_PATH):
        os.makedirs(SRC_DIR, exist_ok=True)
        print("downloading", URL, flush=True)
        r = requests.get(URL, headers=ingest.UA, timeout=600)
        r.raise_for_status()
        with open(ZIP_PATH, "wb") as f:
            f.write(r.content)
    z = zipfile.ZipFile(ZIP_PATH)
    member = z.infolist()[0].filename
    with z.open(member) as f:
        df = pd.read_fwf(io.TextIOWrapper(f, encoding="latin-1"),
                         colspecs=[(s - 1, e) for _, s, e in LAYOUT],
                         names=[n for n, _, _ in LAYOUT], header=None, dtype=str)
    df = df.apply(lambda s: s.str.strip())
    # Numerics are zero-padded with IMPLIED decimals. The layout PDF says Decimal 4 for
    # acres (confirmed: 00010000 = 1.0000 ac suburban lot) but Decimal 0 for values —
    # which is wrong: values are CENTS. Proof: land+building==appraised exactly,
    # assessed==25% of appraised (TN residential statutory ratio), and /100 puts the
    # county median at $288k (vs an absurd $28.8M taken literally).
    df["CalcAcres"] = pd.to_numeric(df["CalcAcres"], errors="coerce") / 10_000
    for c in ("LandValue", "BuildingValue", "AppraisedValue", "AssessedValue"):
        df[c] = pd.to_numeric(df[c], errors="coerce") / 100
    # LUCode arrives 19-wide zero-padded ('0000000000000000111' -> '111')
    lu = df["LUCode"].str.lstrip("0")
    df["LUCode"] = lu.where(lu != "", None)
    df["mail_addr"] = (df[["MailStNum", "MailStDirPfx", "MailStName", "MailTypeSfx",
                           "MailUnit", "MailAddrLine2", "MailCity", "MailState", "MailZip"]]
                       .fillna("").agg(" ".join, axis=1).str.split().str.join(" "))
    gdf = gpd.GeoDataFrame(df, geometry=gpd.GeoSeries([None] * len(df)), crs="EPSG:4326")
    out = os.path.join(RAW, "47065_hamilton.parquet")
    os.makedirs(RAW, exist_ok=True)
    gdf.to_parquet(out, compression="zstd")
    zinfo = z.infolist()[0]
    vintage = "%04d-%02d-%02d" % zinfo.date_time[:3]
    ingest.update_manifest("47065", {
        "fips": "47065", "county": "Hamilton", "state": "TN",
        "file": os.path.relpath(out, ROOT).replace("\\", "/"),
        "feature_count": int(len(gdf)), "reported_count": int(len(gdf)),
        "retrieved_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source_last_edit": vintage, "source_url": URL,
        "owner_fields": "OwnerName1|OwnerName2", "value_field": "AppraisedValue",
        "note": "FREE monthly assessor roll (fixed-width, official layout PDF). ATTRIBUTES ONLY — "
                "county sells geometry for $2,500; rows carry NULL geometry, research-only.",
    })
    print(f"DONE: {len(gdf):,} parcels (no geometry) -> {out}", flush=True)


if __name__ == "__main__":
    main()
