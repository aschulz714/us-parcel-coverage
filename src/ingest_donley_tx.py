"""Donley TX 48129 (Clarendon) — free CAD certified-roll CSV, ATTRIBUTES ONLY.

Donley is absent from the TxGIO StratMap statewide layer and has no GIS REST host;
the CAD's web portal is a Pritchard & Abbott app. But the CAD posts its certified
appraisal roll free (verified by the records-kit session and re-verified 2026-06-12):
  https://donleycad.org/home/DataRecords  -> yearly Real-Personal CSV (PACS export)

Roll mechanics handled here:
  - one row per Account x Ownership_Sequence: co-owner sequences are pipe-joined
    into a single owner string on the primary row
  - PERSONAL-property accounts (PTAD category H*/L*) and mineral-lease rows
    (RRC_Number) are NOT land parcels -> excluded; exempt real property (XV) kept
  - Primary_Category_Code is a PTAD code -> stored as stat_land_use so the existing
    TX prefix-rule table (data/landuse_codes/tx_ptad.csv) categorizes it for free
  - 7 rows have Is_Owner_Confidential=True -> owner replaced with the bracketed
    sentinel [CONFIDENTIAL] (matches the pipeline's placeholder convention)
  - no geometry (county sells none); rows carry NULL geometry, research-only

Usage: python src/ingest_donley_tx.py   (expects the CSV in data/parcels/bulk_src/donley_tx)
"""
import datetime, glob, os, sys

import geopandas as gpd
import pandas as pd

sys.path.insert(0, os.path.dirname(__file__))
import ingest

HERE = os.path.dirname(__file__)
ROOT = os.path.dirname(HERE)
SRC_DIR = os.path.join(ROOT, "data", "parcels", "bulk_src", "donley_tx")
PAGE = "https://donleycad.org/home/DataRecords"
RAW = os.path.join(ROOT, "data", "parcels", "raw")


def _join(parts):
    s = parts.fillna("").astype(str).agg(" ".join, axis=1)
    return s.str.split().str.join(" ")


def main():
    csvs = sorted(glob.glob(os.path.join(SRC_DIR, "*certified_roll.csv")))
    if not csvs:
        sys.exit(f"no roll CSV in {SRC_DIR} — download from {PAGE} first")
    src = csvs[-1]
    df = pd.read_csv(src, dtype=str)
    year = df["Year"].dropna().iloc[0]
    n0 = len(df)
    cat = df["Primary_Category_Code"].fillna("")
    real = ~cat.str.startswith(("H", "L")) & df["RRC_Number"].isna()
    df = df[real]
    # co-owner sequences -> pipe-join names onto one row per account
    df["Ownership_Sequence"] = pd.to_numeric(df["Ownership_Sequence"], errors="coerce")
    df = df.sort_values(["Account", "Ownership_Sequence"])
    extra = (df[df["Ownership_Sequence"] > 1].groupby("Account")["Name"]
             .apply(lambda s: " / ".join(x for x in s.dropna() if x.strip())))
    df = df[df["Ownership_Sequence"] == 1].copy()
    more = df["Account"].map(extra)
    df["owner_name"] = df["Name"].where(more.isna(), df["Name"] + " / " + more)
    df.loc[df["Is_Owner_Confidential"] == "True", "owner_name"] = "[CONFIDENTIAL]"
    df["situs_addr"] = _join(df[["Prop_Street_Number", "Prop_Street_Dir", "Prop_Street",
                                 "Prop_City", "Prop_State", "Prop_Zip5"]])
    df["mail_addr"] = _join(df[["Street", "Street_Overflow", "City", "State", "Zip5"]])
    df["parcel_no"] = df["Account"]
    df["stat_land_use"] = df["Primary_Category_Code"]
    df["Acres"] = pd.to_numeric(df["Acres"], errors="coerce")
    df["Market_Value"] = pd.to_numeric(df["Market_Value"], errors="coerce")
    keep = ["parcel_no", "owner_name", "situs_addr", "mail_addr", "stat_land_use",
            "Acres", "Market_Value", "Legal1", "Legal2", "DBA", "Year"]
    df = df[keep].reset_index(drop=True)
    gdf = gpd.GeoDataFrame(df, geometry=gpd.GeoSeries([None] * len(df)), crs="EPSG:4326")
    out = os.path.join(RAW, "48129_donley.parquet")
    os.makedirs(RAW, exist_ok=True)
    gdf.to_parquet(out, compression="zstd")
    ingest.update_manifest("48129", {
        "fips": "48129", "county": "Donley", "state": "TX",
        "file": os.path.relpath(out, ROOT).replace("\\", "/"),
        "feature_count": int(len(gdf)), "reported_count": int(len(gdf)),
        "retrieved_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source_last_edit": f"{year}-07-01", "source_url": PAGE,
        "owner_fields": "owner_name", "value_field": "Market_Value",
        "note": f"FREE CAD certified roll {year} (PACS CSV). ATTRIBUTES ONLY — no geometry "
                f"anywhere free; absent from TxGIO. Personal-property (H*/L*) and mineral "
                f"rows excluded ({n0} roll rows -> {len(gdf)} real-property parcels).",
    })
    print(f"DONE: {len(gdf):,} real-property parcels (of {n0:,} roll rows) -> {out}", flush=True)


if __name__ == "__main__":
    main()
