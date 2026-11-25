"""GEO ENRICHMENT — derived geography and occupancy columns on normalized parcels, matching the
per-parcel facts commercial vendors (Regrid/Plotzy) surface, from free public geography.

Adds (raw columns never modified):
  census_tract      11-digit GEOID (state 2 + county 3 + tract 6) from a point-in-tract
                    spatial join of each parcel's representative point against the TIGER/Line
                    2023 census-tract polygons. String; NA where the point falls in no tract
                    (offshore, bad geometry) or the county file has no geometry.
  opportunity_zone  nullable bool, compatibility alias for opportunity_zone_2018.
  opportunity_zone_2018
                    Representative-point membership in the pinned official CDFI original-round
                    polygons, independent of modern tract IDs. Missing references, boundary
                    contacts, ambiguous matches and quarantined reference geometry -> NA.
                    This is a geographic indicator, not tax eligibility or whole-parcel coverage.
  owner_occupied    nullable bool, HEURISTIC. True/False = the parcel's mailing street line matches /
                    does not match its situs street line after standardization. NA where either the
                    mailing or situs street is missing/uncomparable. This is the same street-match
                    proxy vendors use, but our inputs are street-line-only (no situs city/state/zip,
                    and situs is sometimes just a house number), so coverage is well under 56% and
                    the flag is conservative (mismatched or partial -> False/NA, never a false True).

Design: manifest-driven, grouped by 2-digit state FIPS so each state's TIGER tract layer loads
once. Per county: pyarrow read -> decode geometry WKB to shapely representative points ->
gpd.sjoin against tracts -> build the three columns index-aligned to row order -> write_columns
(geometry stays opaque, geoparquet metadata preserved, atomic, idempotent). This is the first
enrichment that decodes geometry, so it is materially slower than owners_std/landuse_std.

DURABILITY CAVEAT: like every derived column, these are dropped by a future re-normalize
(normalize.py slices to SCHEMA). Re-run this tool after a re-pull, or wire the columns into
recon/_enrich_rollout.py's restore step. Not added to qa REQUIRED_COLS for that reason.

Usage:
  python src/geo_enrich.py --fips 01001            # one or more counties
  python src/geo_enrich.py --state AZ              # every county in a state
  python src/geo_enrich.py --all                   # national
  python src/geo_enrich.py --fips 01001 --dry-run  # compute + print coverage, write nothing
"""
import argparse, io, json, os, sys, time, urllib.request, zipfile

import geopandas as gpd
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import shapely

from parquet_cols import write_columns
import oz_geography

HERE = os.path.dirname(__file__)
ROOT = os.path.dirname(HERE)
NORM = os.path.join(ROOT, "data", "parcels", "normalized")
MANIFEST = os.path.join(ROOT, "data", "parcels", "manifest.json")
REF = os.path.join(ROOT, "data", "reference")
TIGER_DIR = os.path.join(REF, "tiger")
OZ_JSON = os.path.join(REF, "oz_designated_2018.json")
TIGER_URL = "https://www2.census.gov/geo/tiger/TIGER2023/TRACT/tl_2023_{ss}_tract.zip"

# street type + directional standardization (whole-word, space-padded) — mirrors mailing_std._STREET
_STREET = [(" STREET ", " ST "), (" AVENUE ", " AVE "), (" BOULEVARD ", " BLVD "),
           (" ROAD ", " RD "), (" DRIVE ", " DR "), (" LANE ", " LN "), (" COURT ", " CT "),
           (" PLACE ", " PL "), (" CIRCLE ", " CIR "), (" HIGHWAY ", " HWY "), (" PARKWAY ", " PKWY "),
           (" NORTH ", " N "), (" SOUTH ", " S "), (" EAST ", " E "), (" WEST ", " W "),
           (" NORTHEAST ", " NE "), (" NORTHWEST ", " NW "), (" SOUTHEAST ", " SE "), (" SOUTHWEST ", " SW ")]


def street_key(s):
    """pandas Series[str|NA] -> bare standardized street key (NA where blank). No city/zip
    completion — this is the comparable unit shared by mailing_address and situs_address."""
    x = s.fillna("").astype(str).str.upper()
    x = x.str.replace(r"[^A-Z0-9 ]", " ", regex=True).str.replace(r" +", " ", regex=True).str.strip()
    x = " " + x + " "
    for a, b in _STREET:
        x = x.str.replace(a, b, regex=False)
    x = x.str.replace(r" +", " ", regex=True).str.strip()
    return x.replace("", pd.NA)


def load_oz_set():
    meta = json.load(open(OZ_JSON))
    return set(meta["tracts"])


def tiger_path(ss):
    return os.path.join(TIGER_DIR, f"tl_2023_{ss}_tract.zip")


def ensure_tiger(ss):
    """Download the state's TIGER tract shapefile zip if not cached. Returns local path."""
    p = tiger_path(ss)
    if os.path.exists(p) and os.path.getsize(p) > 0:
        return p
    os.makedirs(TIGER_DIR, exist_ok=True)
    url = TIGER_URL.format(ss=ss)
    tmp = p + ".part"
    urllib.request.urlretrieve(url, tmp)
    os.replace(tmp, p)
    return p


def load_tracts(ss):
    """State tract polygons as a GeoDataFrame[GEOID, geometry] in EPSG:4326."""
    p = ensure_tiger(ss)
    g = gpd.read_file(f"zip://{p}")
    g = g[["GEOID", "geometry"]].copy()
    if g.crs is None:
        g = g.set_crs(4269)          # TIGER is NAD83
    return g.to_crs(4326)


def enrich_table(table, tracts, oz_set=None, oz_boundaries=None):
    """Return row-aligned modern tract, original-round OZ (plus alias), and occupancy."""
    n = table.num_rows
    idx = pd.RangeIndex(n)

    # ---- census_tract via point-in-tract join ----
    tract_ser = pd.Series(pd.array([None] * n, dtype="string"), index=idx)
    if "geometry" in table.column_names and n:
        wkb = table.column("geometry").to_numpy(zero_copy_only=False)
        geoms = shapely.from_wkb(wkb)
        valid = shapely.is_valid(geoms) & ~shapely.is_missing(geoms)
        if valid.any():
            pts = shapely.point_on_surface(geoms[valid])        # always inside the parcel
            # GeoDataFrame aligns a GeoSeries by index labels. Preserve parcel
            # row labels on the series itself: filtering invalid/missing WKB
            # leaves gaps that a default RangeIndex would silently misalign.
            gp = gpd.GeoDataFrame(
                geometry=gpd.GeoSeries(pts, index=idx[valid], crs=4326))
            j = gpd.sjoin(gp, tracts, how="left", predicate="within")
            j = j[~j.index.duplicated(keep="first")]            # boundary points -> keep one
            tract_ser.loc[j.index] = j["GEOID"].astype("string")

    # oz_set is retained only for call compatibility. Modern GEOIDs must never
    # be tested against the legacy designation list. Missing boundaries -> NA.
    oz = oz_geography.classify(table, oz_boundaries, tract_ser.notna().to_numpy())

    # ---- owner_occupied (tri-state, street-match heuristic) ----
    oo = pd.Series(pd.array([None] * n, dtype="boolean"), index=idx)
    cols = table.column_names
    if "mailing_address" in cols and "situs_address" in cols:
        mk = street_key(table.column("mailing_address").to_pandas())
        sk = street_key(table.column("situs_address").to_pandas())
        both = mk.notna() & sk.notna()
        oo[both] = (mk[both] == sk[both])

    return {"census_tract": tract_ser, "opportunity_zone": oz,
            "opportunity_zone_2018": oz.copy(), "owner_occupied": oo}


def cov(ser):
    return f"{ser.notna().mean():.0%}" if len(ser) else "0%"


def has_col(path, col):
    try:
        return col in pq.ParquetFile(path).schema_arrow.names
    except Exception:
        return False


def process(entries, oz_set=None, dry_run=False, skip_existing=False, max_seconds=None):
    # Fail before any county writes if the pinned designation geography is absent.
    oz_boundaries = oz_geography.load_zones(ROOT)
    start = time.monotonic()
    by_state = {}
    for e in entries:
        by_state.setdefault(e["fips"][:2], []).append(e)
    for ss in sorted(by_state):
        if max_seconds and time.monotonic() - start > max_seconds:
            print(f"[budget] {max_seconds}s reached — stopping before state {ss}", flush=True)
            return
        cohort = sorted(by_state[ss], key=lambda x: x["fips"])
        if skip_existing:
            cohort = [e for e in cohort if not all(
                has_col(os.path.join(ROOT, e["normalized_file"]), name)
                for name in ("census_tract", "opportunity_zone_2018"))]
        if not cohort:
            continue
        try:
            tracts = load_tracts(ss)
        except Exception as exc:
            print(f"[{ss}] TIGER load FAILED ({exc}) — skipping {len(cohort)} counties")
            continue
        for e in cohort:
            if max_seconds and time.monotonic() - start > max_seconds:
                print(f"[budget] {max_seconds}s reached — stopping mid-state {ss}", flush=True)
                return
            path = os.path.join(ROOT, e["normalized_file"])
            if not os.path.exists(path):
                continue
            try:
                table = pq.read_table(path)
                newcols = enrich_table(table, tracts, oz_boundaries=oz_boundaries)
                tag = "DRY " if dry_run else ""
                print(f"{tag}{e['fips']} {e['county']}, {e['state']}  n={table.num_rows}  "
                      f"tract={cov(newcols['census_tract'])} oz={cov(newcols['opportunity_zone'])} "
                      f"occ={cov(newcols['owner_occupied'])} "
                      f"oz_true={int(newcols['opportunity_zone'].fillna(False).sum())} "
                      f"occ_true={int(newcols['owner_occupied'].fillna(False).sum())}", flush=True)
                if dry_run:
                    continue
                write_columns(path, table, newcols)
            except Exception as exc:
                print(f"FAIL {e['fips']} {e['county']}, {e['state']}: {type(exc).__name__}: {exc}", flush=True)


def apply_county(fips, oz_set=None):
    """Re-derive geography columns for ONE county, in-process. Called by the re-normalize
    restore paths (refresh.py, _enrich_rollout.py) so a re-pull never leaves the county
    geo-degraded — the direct parallel to owners_std.apply_files / landuse_std.apply_county.
    Loads the county's state TIGER layer + OZ set. No-op (warn) if the manifest lacks the fips."""
    manifest = json.load(open(MANIFEST))
    e = manifest.get(fips)
    if not e or not e.get("normalized_file"):
        print(f"  geo_enrich {fips}: no manifest/normalized_file")
        return
    process([dict(e, fips=fips)], oz_set)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fips", nargs="+", help="one or more 5-digit county FIPS")
    ap.add_argument("--state", help="2-letter state code")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--skip-existing", action="store_true",
                    help="skip counties already containing modern tracts and the versioned 2018 OZ flag")
    ap.add_argument("--max-seconds", type=float, default=None,
                    help="stop launching new counties after this many seconds (chunked runs)")
    args = ap.parse_args()

    manifest = json.load(open(MANIFEST))
    entries = [dict(e, fips=fips) for fips, e in manifest.items() if e.get("normalized_file")]
    if args.fips:
        want = set(args.fips)
        entries = [e for e in entries if e["fips"] in want]
    elif args.state:
        entries = [e for e in entries if e["state"] == args.state.upper()]
    elif not args.all:
        ap.error("specify --fips, --state, or --all")
    if not entries:
        print("no matching counties")
        return
    print(f"OZ: pinned original-round polygons | counties: {len(entries)}", flush=True)
    process(entries, dry_run=args.dry_run,
            skip_existing=args.skip_existing, max_seconds=args.max_seconds)


if __name__ == "__main__":
    main()
