"""PMTiles builder — the scalable map tier (replaces multi-GB per-state FlatGeobufs).

For each state with normalized data, produces output/parcels_<st>.pmtiles via GDAL's
PMTiles driver (ogr2ogr from the isolated `gdal_tiles` conda env — the base env's
pyogrio/GDAL is untouched). Tiles carry the popup fields only; geometry simplifies at
low zoom and MapLibre overzooms past MAXZOOM, so a state lands at ~5-15% of its FGB size.

Source preference: the state's existing FGB in output/ (already popup-trimmed), else a
TEMPORARY FGB streamed county-by-county from normalized parquet (append mode — Texas
never sits in RAM), deleted after tiling.

Usage:
  python src/build_tiles.py --state WY            # one state
  python src/build_tiles.py --all                 # every state in the store
  python src/build_tiles.py --all --replace-fgb   # also delete the FGB after a verified build
"""
import argparse, glob, json, os, subprocess, sys

import geopandas as gpd

from atomic_io import atomic_write_json
from publication_policy import DEFAULT_POLICY_PATH, filter_fields, load_policy

HERE = os.path.dirname(__file__)
ROOT = os.path.dirname(HERE)
NORM = os.path.join(ROOT, "data", "parcels", "normalized")
OUT = os.path.join(ROOT, "output")
OGR2OGR = ["conda", "run", "-n", "gdal_tiles", "ogr2ogr"]
KEEP = ["state", "county_fips", "county_name", "apn", "owner", "owner_norm", "owner_family",
        "situs_address", "assessed_value", "land_use", "land_use_code", "land_use_category",
        "acreage", "source_vintage"]
# z12-14 only: tiles get ~4x fewer per dropped top zoom, MapLibre overzooms past 14
# (z14 extent 4096 = sub-meter precision), and county fill covers below 12
MINZOOM, MAXZOOM = "12", "14"


def state_files(st2):
    return sorted(glob.glob(os.path.join(NORM, f"{st2}*.parquet")))


def temp_vrt_for(st, st2):
    """A VRT union layer over the state's normalized parquets — ogr2ogr streams each
    county straight into the tiler. (The old per-county FGB-append approach rewrote the
    whole file per county — quadratic I/O — and is gone.) The gdal_tiles env carries the
    Parquet driver (libgdal-arrow-parquet)."""
    files = state_files(st2)
    parts = []
    for p in files:
        parts.append(
            f'    <OGRVRTLayer name="{os.path.splitext(os.path.basename(p))[0]}">\n'
            f"      <SrcDataSource>{os.path.abspath(p)}</SrcDataSource>\n"
            f"    </OGRVRTLayer>")
    vrt = (f'<OGRVRTDataSource>\n  <OGRVRTUnionLayer name="parcels">\n'
           + "\n".join(parts) + "\n  </OGRVRTUnionLayer>\n</OGRVRTDataSource>\n")
    tmp = os.path.join(OUT, f"_tiles_src_{st}.vrt")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(vrt)
    print(f"    vrt union over {len(files)} county parquets", flush=True)
    return tmp


def build_state(st, replace_fgb=False, policy_path=DEFAULT_POLICY_PATH):
    st2 = None
    for p in glob.glob(os.path.join(NORM, "*.parquet")):
        g = os.path.basename(p)
        # state code from any county of this state via states map in crosswalk fips prefix
    import csv
    with open(os.path.join(HERE, "county_methods.csv"), encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r["state"] == st.upper():
                st2 = r["fips"][:2]
                break
    if not st2 or not state_files(st2):
        print(f"  {st}: no normalized data — skipped")
        return False
    # always tile from the VRT over normalized parquet — the old FGBs predate the
    # owner_norm/owner_family columns and would ship stale tiles
    existing_fgb = os.path.join(OUT, f"parcels_{st.lower()}.fgb")
    src = temp_vrt_for(st.lower(), st2)
    out = os.path.join(OUT, f"parcels_{st.lower()}.pmtiles")
    tmp_out = out + ".building"
    if os.path.exists(tmp_out):
        os.unlink(tmp_out)
    # select only fields that actually exist in this state's schema (land_use_code/
    # category are WA-only until other states get land-use standards)
    import pyarrow.parquet as pq
    have = set(pq.ParquetFile(state_files(st2)[0]).schema_arrow.names)
    policy = load_policy(policy_path)
    select, suppressed, policy_rule = filter_fields(policy, st, KEEP, have)
    if suppressed:
        print(f"    publication policy suppresses: {', '.join(suppressed)}", flush=True)
    # -skipfailures: null-geometry assessor records (kept in parquet by design) raise
    # 'Invalid bounds' in the tile writer — skipping them mirrors the map's old behavior
    cmd = OGR2OGR + ["-f", "PMTiles", tmp_out, src, "-nln", "parcels",
                     "-select", ",".join(select), "-skipfailures",
                     "-dsco", f"MINZOOM={MINZOOM}", "-dsco", f"MAXZOOM={MAXZOOM}",
                     "-dsco", f"NAME=parcels_{st.lower()}"]
    print(f"  {st}: tiling {os.path.basename(src)} -> {os.path.basename(out)} ...", flush=True)
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0 or not os.path.exists(tmp_out):
        print(f"  {st} FAILED (full stderr):\n{r.stderr}")
        return False
    if os.path.exists(out):
        os.unlink(out)
    os.replace(tmp_out, out)  # a failed build can never destroy a working tileset
    mb = os.path.getsize(out) / 1e6
    print(f"  {st}: {mb:,.0f} MB pmtiles", flush=True)
    if src != existing_fgb and os.path.exists(src):
        os.unlink(src)
    if replace_fgb and os.path.exists(existing_fgb):
        os.unlink(existing_fgb)
        print(f"  {st}: removed {os.path.basename(existing_fgb)}", flush=True)
    # register in states.json (pmtiles key; bbox entry already exists for fgb states)
    sj_path = os.path.join(OUT, "states.json")
    sj = json.load(open(sj_path, encoding="utf-8")) if os.path.exists(sj_path) else {}
    entry = sj.setdefault(st.lower(), {})
    entry["pmtiles"] = os.path.basename(out)
    entry["published_fields"] = select
    entry["suppressed_fields"] = suppressed
    entry["publication_policy_version"] = policy["schema_version"]
    if policy_rule.get("review_required"):
        entry["publication_review_required"] = True
    if replace_fgb or not os.path.exists(existing_fgb):
        entry.pop("file", None)
    atomic_write_json(sj_path, sj)
    return True


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--state")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--replace-fgb", action="store_true")
    ap.add_argument("--policy", default=DEFAULT_POLICY_PATH,
                    help="field-level publication policy JSON")
    args = ap.parse_args()
    if args.state:
        states = [args.state]
    elif args.all:
        import csv
        with open(os.path.join(HERE, "county_methods.csv"), encoding="utf-8") as f:
            by2 = {r["fips"][:2]: r["state"] for r in csv.DictReader(f)}
        seen = sorted({by2[os.path.basename(p)[:2]] for p in glob.glob(os.path.join(NORM, "*.parquet"))
                       if os.path.basename(p)[:2] in by2})
        states = seen
    else:
        sys.exit("--state XX or --all")
    print(f"Building PMTiles for: {states}")
    for st in states:
        build_state(st, replace_fgb=args.replace_fgb, policy_path=args.policy)


if __name__ == "__main__":
    main()
