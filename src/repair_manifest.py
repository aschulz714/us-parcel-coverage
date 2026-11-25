"""Repair manifest entries lost to write races (pre-lockfile concurrent state ingests).

The raw parquet files are the ground truth: any raw/<fips>_*.parquet without a manifest
entry gets its entry rebuilt from the crosswalk row + the file itself (feature_count from
the parquet, retrieved_at from file mtime — slightly later than the true pull, labeled so).

Usage:  python src/repair_manifest.py
"""
import csv, datetime, glob, os, sys

import geopandas as gpd

sys.path.insert(0, os.path.dirname(__file__))
import ingest

HERE = os.path.dirname(__file__)
ROOT = os.path.dirname(HERE)
RAW = os.path.join(ROOT, "data", "parcels", "raw")
MANIFEST = os.path.join(ROOT, "data", "parcels", "manifest.json")


def main():
    with open(os.path.join(HERE, "county_methods.csv"), encoding="utf-8") as f:
        xw = {r["fips"]: r for r in csv.DictReader(f)}
    manifest = ingest.load_manifest(MANIFEST)
    repaired = 0
    for p in sorted(glob.glob(os.path.join(RAW, "*.parquet"))):
        fips = os.path.basename(p)[:5]
        if fips in manifest and manifest[fips].get("file"):
            continue
        row = xw.get(fips)
        if not row:
            print(f"  SKIP {fips}: not in crosswalk")
            continue
        import pyarrow.parquet as pq
        n = pq.ParquetFile(p).metadata.num_rows
        mtime = datetime.datetime.fromtimestamp(os.path.getmtime(p), datetime.timezone.utc)
        ingest.update_manifest(fips, {
            "fips": fips, "county": row["county"], "state": row["state"],
            "file": os.path.relpath(p, ROOT).replace("\\", "/"),
            "feature_count": int(n), "reported_count": None,
            "retrieved_at": mtime.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "source_last_edit": None, "source_url": row["query_url"],
            "owner_fields": row["owner_fields"], "value_field": row["value_field"],
            "note": (row["note"] + " " if row["note"] else "") +
                    "[manifest entry REPAIRED from raw file after a write race; retrieved_at = file mtime]",
        }, MANIFEST)
        print(f"  REPAIRED {fips} {row['county']} {row['state']}: {n:,} parcels")
        repaired += 1
    print(f"{repaired} entries repaired")


if __name__ == "__main__":
    main()
