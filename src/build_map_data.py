"""Build the research-map data files from the normalized parcel store.

Outputs (consumed by output/parcel_map.html, served by src/serve_map.py):
  output/parcels_<st>.fgb         — ALL normalized parcels for the state as ONE FlatGeobuf.
                                    FGB has a built-in spatial index and streams over HTTP
                                    range requests, so the browser fetches only the parcels
                                    in the current viewport — static hosting, no tile server.
                                    (For the national build, swap to tippecanoe->PMTiles on
                                    a Linux/WSL box; same HTML, different source.)
  output/counties_status.geojson  — county polygons + ingest status (parcel count, vintage,
                                    crosswalk method) for the low-zoom coverage view.

Usage:
  python src/build_map_data.py --state WA
"""
import argparse, csv, glob, json, os, sys

import geopandas as gpd
import pandas as pd

HERE = os.path.dirname(__file__)
ROOT = os.path.dirname(HERE)
NORM = os.path.join(ROOT, "data", "parcels", "normalized")
MANIFEST = os.path.join(ROOT, "data", "parcels", "manifest.json")
COUNTIES = os.path.join(ROOT, "data", "us_counties.geojson")
CROSSWALK = os.path.join(HERE, "county_methods.csv")
OUT = os.path.join(ROOT, "output")


def build_parcels_fgb(state):
    with open(CROSSWALK, encoding="utf-8") as f:
        state_fips = {r["fips"] for r in csv.DictReader(f) if r["state"].upper() == state}
    # glob the store directly (the manifest can lag while an ingest run is in flight)
    files = sorted(p for p in glob.glob(os.path.join(NORM, "*.parquet"))
                   if os.path.basename(p)[:5] in state_fips)
    if not files:
        sys.exit(f"no normalized counties for {state} — run ingest.py + normalize.py first")
    print(f"Merging {len(files)} counties...")
    gdf = pd.concat([gpd.read_parquet(p) for p in files], ignore_index=True)
    # FGB attribute payload stays lean: drop fields the popup doesn't show
    gdf = gdf.drop(columns=["source_url", "retrieved_at", "mailing_address"])
    # geometry-less assessor records (condos etc.) can't join a spatially-indexed FGB;
    # they remain queryable in the parquet store via research.py
    n0 = len(gdf)
    gdf = gdf[gdf.geometry.notna() & ~gdf.geometry.is_empty]
    if n0 - len(gdf):
        print(f"  dropped {n0 - len(gdf)} geometry-less records from the map file (still in parquet)")
    # world-spanning degenerate rings sit invisibly over the whole map and steal clicks
    # (the Lincoln statewide-slice bug) — belt-and-suspenders here in case a future county
    # ships them; legit county-wide aggregates (waterways/roads) stay well under 5 degrees
    b = gdf.geometry.bounds
    junk = ((b.maxx - b.minx) > 5) | ((b.maxy - b.miny) > 5)
    if junk.any():
        print(f"  dropped {int(junk.sum())} degenerate world-spanning geometries")
        gdf = gdf[~junk]
    # FlatGeobuf can't mix 2D/3D geometries in one file; some counties ship Z coords
    # (e.g. PA Franklin/Lackawanna) — flatten to 2D so the merged state file is uniform.
    if gdf.geometry.has_z.any():
        n3 = int(gdf.geometry.has_z.sum())
        try:
            gdf = gdf.set_geometry(gdf.geometry.force_2d())
        except AttributeError:
            from shapely import force_2d
            gdf = gdf.set_geometry(gdf.geometry.apply(force_2d))
        print(f"  flattened {n3} 3D geometries to 2D")
    out_path = os.path.join(OUT, f"parcels_{state.lower()}.fgb")
    os.makedirs(OUT, exist_ok=True)
    gdf.to_file(out_path, driver="FlatGeobuf")
    mb = os.path.getsize(out_path) / 1e6
    print(f"  {len(gdf):,} parcels -> {os.path.relpath(out_path, ROOT)} ({mb:,.0f} MB)")
    return len(gdf)


def build_county_status(state):
    with open(MANIFEST, encoding="utf-8") as f:
        manifest = json.load(f)
    with open(CROSSWALK, encoding="utf-8") as f:
        methods = {r["fips"]: r["method"] for r in csv.DictReader(f)}
    with open(COUNTIES, encoding="utf-8") as f:
        cj = json.load(f)
    feats = []
    for feat in cj["features"]:
        p = feat["properties"]
        fips = p["STATE"] + p["COUNTY"]
        e = manifest.get(fips)
        pop = (e or {}).get("populated") or {}
        src = (e or {}).get("source_url") or ""
        host = src.split("/")[2] if "://" in src else ""
        feat["properties"] = {
            "fips": fips, "name": p["NAME"], "state": (e or {}).get("state"),
            "method": methods.get(fips, "?"),
            "ingested": bool(e and e.get("normalized_file")),
            # geometry-only (yellow): ingested but no owner AND no value attributes
            "geom_only": bool(e and e.get("normalized_file")
                              and not round(pop.get("owner") or 0, 3)
                              and not round(pop.get("assessed_value") or 0, 3)),
            "parcels": e["feature_count"] if e else None,
            "vintage": (e or {}).get("source_last_edit"),
            "retrieved": (e or {}).get("retrieved_at", "")[:10] or None,
            # coverage fractions (0-1) for the map's "color counties by" explorer + data sheet
            "owner_cov": round(pop.get("owner") or 0, 3),
            "value_cov": round(pop.get("assessed_value") or 0, 3),
            "situs_cov": round(pop.get("situs_address") or 0, 3),
            "mail_cov": round(pop.get("mailing_address") or 0, 3),
            "landuse_cov": round((e or {}).get("landuse_coverage") or 0, 3),
            "source": host,
        }
        feats.append(feat)
    out_path = os.path.join(OUT, "counties_status.geojson")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"type": "FeatureCollection", "features": feats}, f)
    n = sum(1 for x in feats if x["properties"]["ingested"])
    print(f"  county status: {n} ingested counties flagged -> {os.path.relpath(out_path, ROOT)}")


def build_states_index():
    """output/states.json: every state with a parcels_<st>.fgb present, plus its bbox
    (from county geometry) — the map streams from EVERY state file whose bbox intersects
    the viewport, so multi-state browsing needs no ?state= parameter."""
    with open(COUNTIES, encoding="utf-8") as f:
        cj = json.load(f)
    with open(CROSSWALK, encoding="utf-8") as f:
        st_by_fips2 = {r["fips"][:2]: r["state"] for r in csv.DictReader(f)}
    bounds = {}
    for feat in cj["features"]:
        st = st_by_fips2.get(feat["properties"]["STATE"])
        if not st:
            continue
        def walk(coords):
            if isinstance(coords[0], (int, float)):
                yield coords
            else:
                for c in coords:
                    yield from walk(c)
        xs, ys = zip(*walk(feat["geometry"]["coordinates"]))
        b = bounds.setdefault(st, [180, 90, -180, -90])
        b[0], b[1] = min(b[0], min(xs)), min(b[1], min(ys))
        b[2], b[3] = max(b[2], max(xs)), max(b[3], max(ys))
    index = {}
    for ext, key in ((".pmtiles", "pmtiles"), (".fgb", "file")):
        for p in sorted(glob.glob(os.path.join(OUT, f"parcels_*{ext}"))):
            st = os.path.basename(p)[len("parcels_"):-len(ext)].upper()
            if st in bounds:
                entry = index.setdefault(st.lower(), {"bbox": [round(v, 3) for v in bounds[st]]})
                entry[key] = os.path.basename(p)
    with open(os.path.join(OUT, "states.json"), "w", encoding="utf-8") as f:
        json.dump(index, f, indent=1)
    print(f"  states index: {sorted(index)} -> output/states.json")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--state")
    ap.add_argument("--index-only", action="store_true",
                    help="just regenerate states.json from the fgb files already in output/")
    args = ap.parse_args()
    if not args.index_only:
        if not args.state:
            sys.exit("--state required (or use --index-only)")
        state = args.state.upper()
        build_parcels_fgb(state)
        build_county_status(state)
    build_states_index()
    print(f"\nView it:  python src/serve_map.py   then open http://localhost:8008/parcel_map.html")


if __name__ == "__main__":
    main()
