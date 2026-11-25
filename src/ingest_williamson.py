"""Williamson TN 47187 — KEYSET pagination one-off.

The host (arcgis2.williamson-tn.org) throttles objectIds batch POSTs to a crawl but serves
keyset GETs (WHERE OBJECTID > cursor ORDER BY OBJECTID) at ~2.6s/1000-row page (measured by
the recon session, recon/next_states.md §4). No supportsPagination needed.

Usage: python src/ingest_williamson.py   (writes raw parquet + manifest entry)
"""
import datetime, io, os, sys, time, warnings

warnings.filterwarnings("ignore")
import geopandas as gpd
import pandas as pd
import requests

sys.path.insert(0, os.path.dirname(__file__))
import ingest

HERE = os.path.dirname(__file__)
ROOT = os.path.dirname(HERE)
URL = "https://arcgis2.williamson-tn.org/arcgis/rest/services/IDT/DataPull/MapServer/4/query"
RAW = os.path.join(ROOT, "data", "parcels", "raw")


def main():
    sess = requests.Session()
    pages, cursor, n = [], -1, 0
    while True:
        last = None
        for attempt in range(4):
            try:
                r = sess.get(URL, params={
                    "where": f"OBJECTID > {cursor}", "orderByFields": "OBJECTID",
                    "outFields": "*", "outSR": 4326, "resultRecordCount": 1000, "f": "json"},
                    headers=ingest.UA, timeout=180)
                r.raise_for_status()
                d = r.json()
                if d.get("error"):
                    raise RuntimeError(d["error"].get("message"))
                break
            except Exception as e:
                last = e
                time.sleep(3 * (attempt + 1))
        else:
            raise RuntimeError(f"keyset page failed at cursor {cursor}: {last}")
        feats = d.get("features", [])
        if not feats:
            break
        pages.append(gpd.read_file(io.BytesIO(r.content)))
        cursor = max(f["attributes"]["OBJECTID"] for f in feats)
        n += len(feats)
        print(f"  Williamson keyset: {n:,} rows (cursor {cursor})", flush=True)
        time.sleep(0.2)
    gdf = pd.concat([p.set_crs(4326, allow_override=True) for p in pages], ignore_index=True)
    if gdf.columns.duplicated().any():
        gdf = gdf.loc[:, ~gdf.columns.duplicated()]
    out = os.path.join(RAW, "47187_williamson.parquet")
    os.makedirs(RAW, exist_ok=True)
    gdf.to_parquet(out, compression="zstd")
    ingest.update_manifest("47187", {
        "fips": "47187", "county": "Williamson", "state": "TN",
        "file": os.path.relpath(out, ROOT).replace("\\", "/"),
        "feature_count": int(len(gdf)), "reported_count": 100369,
        "retrieved_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source_last_edit": None, "source_url": URL,
        "owner_fields": "Owner", "value_field": "",
        "note": "KEYSET-paged (host throttles objectIds batches; OBJECTID-cursor GETs are fast — recon 2026-06-12).",
    })
    print(f"DONE: {len(gdf):,} parcels -> {out}", flush=True)


if __name__ == "__main__":
    main()
