"""Texas statewide bulk pull — the per-county `where` route is unusable here.

The TX crosswalk routes all 253 counties at one 14.3M-row statewide MapServer
(TxGIO stratmap_land_parcels_48_most_recent). Per-county `where` filters force full
scans (hours per county); indexed objectid-range pages return in ~4.5 s. So: pull the
whole layer once by objectid range (3 workers, checkpointed), then split into
per-county raw parquets named like ingest.py output + manifest entries.

Usage:
    python src/ingest_bulk_tx.py            # pull (resumable) + split + manifest
    python src/ingest_bulk_tx.py --split-only
"""
import argparse
import glob
import json
import os
import time
import warnings
from concurrent.futures import ThreadPoolExecutor

warnings.filterwarnings("ignore")
import geopandas as gpd
import pandas as pd
import requests

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
URL = ("https://feature.geographic.texas.gov/arcgis/rest/services/Parcels/"
       "stratmap_land_parcels_48_most_recent/MapServer/0/query")
CHUNK = 2000          # layer maxRecordCount
PAGES_PER_FILE = 10   # 20k rows per chunk parquet
WORKERS = 3
CHUNK_DIR = os.path.join(HERE, "data", "parcels", "bulk_src", "tx_chunks")
RAW = os.path.join(HERE, "data", "parcels", "raw")
MANIFEST = os.path.join(HERE, "data", "parcels", "manifest.json")


def get_max_oid():
    js = requests.get(URL, params={"where": "1=1", "returnCountOnly": "true", "f": "json"},
                      timeout=60, verify=False).json()
    return js["count"]


def fetch_block(start):
    """Fetch PAGES_PER_FILE pages beginning at objectid `start`; save one parquet."""
    out = os.path.join(CHUNK_DIR, f"chunk_{start:09d}.parquet")
    if os.path.exists(out):
        return 0
    frames = []
    for p in range(PAGES_PER_FILE):
        lo = start + p * CHUNK
        hi = lo + CHUNK
        for attempt in range(4):
            try:
                js = requests.get(URL, params={
                    "where": f"objectid >= {lo} AND objectid < {hi}",
                    "outFields": "*", "f": "geojson"}, timeout=180, verify=False).json()
                feats = js.get("features", [])
                if feats:
                    frames.append(gpd.GeoDataFrame.from_features(feats, crs="EPSG:4326"))
                break
            except Exception:
                if attempt == 3:
                    with open(os.path.join(CHUNK_DIR, "failed.txt"), "a") as f:
                        f.write(f"{lo}\n")
                time.sleep(2 ** attempt)
        time.sleep(0.1)
    if frames:
        gdf = gpd.GeoDataFrame(pd.concat(frames, ignore_index=True), crs="EPSG:4326")
        gdf.to_parquet(out)
        return len(gdf)
    # write empty marker so resume skips
    gpd.GeoDataFrame({"geometry": []}, crs="EPSG:4326").to_parquet(out)
    return 0


def pull():
    os.makedirs(CHUNK_DIR, exist_ok=True)
    max_oid = get_max_oid()
    block = CHUNK * PAGES_PER_FILE
    starts = list(range(1, max_oid + 1, block))
    todo = [s for s in starts
            if not os.path.exists(os.path.join(CHUNK_DIR, f"chunk_{s:09d}.parquet"))]
    print(f"max objectid {max_oid:,}; {len(starts)} blocks total, {len(todo)} to fetch", flush=True)
    done, t0 = 0, time.time()
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for n in ex.map(fetch_block, todo):
            done += 1
            if done % 20 == 0:
                rate = done / (time.time() - t0)
                print(f"  {done}/{len(todo)} blocks  {rate*60:.1f}/min  "
                      f"ETA {(len(todo)-done)/rate/3600:.1f}h", flush=True)
    print("pull complete", flush=True)


def split():
    """Two-stage, memory-safe: chunk -> per-county pieces -> per-county concat."""
    piece_root = os.path.join(CHUNK_DIR, "..", "tx_split")
    os.makedirs(piece_root, exist_ok=True)
    files = sorted(glob.glob(os.path.join(CHUNK_DIR, "chunk_*.parquet")))
    print(f"stage 1: splitting {len(files)} chunk files into county pieces...", flush=True)
    for i, fp in enumerate(files):
        marker = os.path.join(piece_root, f".done_{os.path.basename(fp)}")
        if os.path.exists(marker):
            continue
        g = gpd.read_parquet(fp)
        if len(g):
            g["fips"] = g["fips"].astype(str).str.zfill(5) if "fips" in g.columns else "00000"
            for fips, sub in g.groupby("fips"):
                d = os.path.join(piece_root, fips)
                os.makedirs(d, exist_ok=True)
                sub.to_parquet(os.path.join(d, os.path.basename(fp)))
        open(marker, "w").close()
        if (i + 1) % 100 == 0:
            print(f"  {i+1}/{len(files)}", flush=True)

    print("stage 2: per-county concat -> raw + manifest...", flush=True)
    ts = pd.Timestamp.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    for d in sorted(glob.glob(os.path.join(piece_root, "48*"))):
        fips = os.path.basename(d)
        pieces = [gpd.read_parquet(p) for p in sorted(glob.glob(os.path.join(d, "*.parquet")))]
        if not pieces:
            continue
        gdf = gpd.GeoDataFrame(pd.concat(pieces, ignore_index=True), crs="EPSG:4326")
        cname = str(gdf["county"].mode().iat[0]).strip().lower().replace(" ", "_") \
            if "county" in gdf.columns and len(gdf) else fips
        out_file = os.path.join(RAW, f"{fips}_{cname}.parquet")
        gdf.to_parquet(out_file)
        # ALWAYS write the manifest through the lock-guarded helper — plain json.dump
        # races against concurrent state ingests (the Gallatin MT lesson).
        from ingest import update_manifest
        update_manifest(fips, {
            "fips": fips, "county": cname.replace("_", " ").title(), "state": "TX",
            "file": f"data/parcels/raw/{fips}_{cname}.parquet",
            "feature_count": len(gdf), "reported_count": len(gdf),
            "retrieved_at": ts, "source_last_edit": None, "source_url": URL,
            "owner_fields": "owner_name", "value_field": "mkt_value",
            "note": "bulk statewide objectid-paged pull",
        })
        print(f"  {fips} {cname}: {len(gdf):,}", flush=True)
    print("split complete", flush=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--split-only", action="store_true")
    args = ap.parse_args()
    if not args.split_only:
        pull()
    split()
