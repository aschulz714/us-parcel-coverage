"""Build a national "major owners" parcel layer for local mapping (QGIS): the parcels owned by
LARGE owners, tagged with a category (Federal / State / Institutional). Government land falls out
naturally (USA/BLM/USFS/State-of-X are the biggest owners). Local-gov (city/county) is categorized
but EXCLUDED per scope (Federal + State + top institutional).

Federal/State are categorized from the per-parcel OWNER NAME (owner_norm) directly, so they are
COMPLETE for every county including freshly-ingested states (owner_norm is ~100% populated, while
the curated owner_family rollup lags new ingests). Institutional uses owner_family >= a national
parcel threshold (the big SFR/homebuilder/utility owners; computed once in --report).

Usage:
  python src/major_owners.py --report [--min 1000]   # institutional threshold + write target_families.parquet
  python src/major_owners.py --extract               # scan normalized -> data/parcels/owners/major_owners.parquet (geoparquet)
"""
import argparse
import os
import re

import duckdb

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OWN = os.path.join(ROOT, "data", "parcels", "owners")
SUMMARY = os.path.join(OWN, "owner_summary.parquet")
NORM = os.path.join(ROOT, "data", "parcels", "normalized", "*.parquet")
TARGET = os.path.join(OWN, "major_owners_target.parquet")
OUT = os.path.join(OWN, "major_owners.parquet")

STATE_NAMES = sorted({
    "ALABAMA", "ALASKA", "ARIZONA", "ARKANSAS", "CALIFORNIA", "COLORADO", "CONNECTICUT",
    "DELAWARE", "FLORIDA", "GEORGIA", "HAWAII", "IDAHO", "ILLINOIS", "INDIANA", "IOWA",
    "KANSAS", "KENTUCKY", "LOUISIANA", "MAINE", "MARYLAND", "MASSACHUSETTS", "MICHIGAN",
    "MINNESOTA", "MISSISSIPPI", "MISSOURI", "MONTANA", "NEBRASKA", "NEVADA", "OHIO",
    "OKLAHOMA", "OREGON", "PENNSYLVANIA", "TENNESSEE", "TEXAS", "UTAH", "VERMONT",
    "VIRGINIA", "WASHINGTON", "WISCONSIN", "WYOMING", "NORTH CAROLINA", "SOUTH CAROLINA",
    "NORTH DAKOTA", "SOUTH DAKOTA", "NEW MEXICO", "NEW YORK", "NEW JERSEY", "NEW HAMPSHIRE",
    "RHODE ISLAND", "WEST VIRGINIA",
}, key=len, reverse=True)  # longest-first so 'NORTH DAKOTA' matches before 'NORTH'

# --- pattern STRINGS (RE2-compatible; reused for both Python re and DuckDB regexp_matches) ---
FED_EXACT = {"USA", "U S A", "U.S.A.", "US GOVERNMENT", "US GOVT", "UNITED STATES",
             "UNITED STATES OF AMERICA"}
# 'UNITED STATES' only when followed by a govt word (not 'UNITED STATES STEEL'); FOREST/FISH only
# with 'SERVICE'; '^USA ' prefix is federal (USA PUBLIC DOMAIN) but 'CHEVRON USA' (suffix) is not.
P_FED = (r"(^USA |UNITED STATES (OF AMERICA|GOVERNMENT|GOVT|POSTAL|DEPARTMENT|DEPT|FOREST|FISH|ARMY|NAVY|AIR FORCE)|"
         r"BUREAU OF LAND|\bBLM\b|FOREST SERVICE|"
         r"NATIONAL (PARK|FOREST|WILDLIFE REFUGE|MONUMENT|GRASSLAND|RECREATION)|"
         r"FISH (AND |& |)WILDLIFE SERVICE|BUREAU OF (INDIAN|RECLAMATION)|"
         r"DEPARTMENT OF (THE )?(INTERIOR|DEFENSE|VETERANS AFFAIRS)|"
         r"CORPS OF ENGINEERS|TENNESSEE VALLEY AUTHORITY|\bTVA\b|\bUSDA\b|"
         r"GENERAL SERVICES ADMINISTRATION|NATIONAL AERONAUTICS|US POSTAL|US ARMY|US NAVY|US AIR FORCE)")
P_NOTFED = r"(FEDERAL NATIONAL MORTGAGE|FEDERAL HOME LOAN|FANNIE|FREDDIE)"
P_STRONGFED = r"(^USA |\bUSA\b|UNITED STATES|\bUSDA\b|US POSTAL|US ARMY|US NAVY|US AIR FORCE)"
# state agencies unambiguous enough to mark State on their own (federal DOT/etc. is caught by P_FED
# first via the CASE order). NO bare DOT (matches 'BIGFOOT DOT COM').
P_STATEAGENCY = (r"(DEPARTMENT OF TRANSPORTATION|DEPARTMENT OF NATURAL RESOURCES|\bDNR\b|\bD N R\b|"
                 r"STATE UNIVERSITY|BOARD OF REGENTS|STATE LAND|TRUST LAND|STATE TRUST|"
                 r"GAME (AND |& |)(FISH|PARKS?)|FISH (AND |& |)GAME|PARKS (AND |& |)WILDLIFE|"
                 r"WILDLIFE (RESOURCES|COMMISSION|MANAGEMENT AREA)|DEPARTMENT OF FISH|STATE PARK|"
                 r"TURNPIKE|STATE HIGHWAY|DEPARTMENT OF WILDLIFE|DEPARTMENT OF CONSERVATION|STATE OF)")
P_LOCAL = (r"(CITY OF|TOWN OF|VILLAGE OF|COUNTY OF|\bCOUNTY\b|TOWNSHIP|BOROUGH OF|SCHOOL DIST|"
           r"HOUSING AUTHORITY|FLOOD CONTROL|WATER (DISTRICT|CONSERVANCY|AUTHORITY)|PORT (OF|AUTHORITY)|"
           r"MUNICIPAL|METROPOLITAN|PARK DISTRICT|TRANSIT|REDEVELOPMENT|IRRIGATION DIST|UTILITY DIST|"
           r"FIRE (DISTRICT|PROTECTION)|SANITARY|DRAINAGE DIST|CONSERVATION DIST|LAND ?BANK|LAND REUTIL)"
           )
P_STATENAME = "(" + "|".join(STATE_NAMES) + ")"

FED = re.compile(P_FED)
NOT_FED = re.compile(P_NOTFED)
STRONG_FED = re.compile(P_STRONGFED)
STATE_AGENCY = re.compile(P_STATEAGENCY)
LOCAL = re.compile(P_LOCAL)
STATENAME = re.compile(P_STATENAME)


# placeholder / non-owner values assessors use when the real owner is unknown/withheld/truncated.
# EXACT-match only (so real short brands like 'JJJ' are NOT excluded).
PLACEHOLDER_OWNERS = {
    "STATE", "OWNER", "OWNERS", "OWNER UNKNOWN", "UNKNOWN", "UNKNOWN OWNER", "OWNER NOT ON FILE",
    "NOT ON FILE", "NO NAME", "NONE", "N A", "NA", "SAME", "SAME AS ABOVE", "CURRENT OWNER",
    "CURRENT RESIDENT", "OCCUPANT", "TAXPAYER", "TAX PAYER", "NO OWNER", "NOT AVAILABLE",
    "WITHHELD", "REDACTED", "CONFIDENTIAL", "UNDISCLOSED", "VACANT", "MULTIPLE OWNERS", "VARIOUS",
    "VARIOUS OWNERS", "PRIVATE OWNER", "PRIVATE", "TBD", "TO BE DETERMINED", "DELETED", "VOID",
}


def is_placeholder(name):
    return (name or "").upper().strip() in PLACEHOLDER_OWNERS


def _state_sig(f):
    return bool(re.search(r"\bSTATE OF\b", f)) or bool(re.search(r"COMMONWEALTH OF", f)) \
        or (bool(STATENAME.search(f)) and bool(re.search(r"\bSTATE\b", f))) \
        or bool(STATE_AGENCY.search(f))


def categorize(name):
    """Federal / State / Local / Institutional from an owner NAME (owner_norm or owner_family)."""
    if not name:
        return None
    f = name.upper().strip()
    if f in FED_EXACT:
        return "Federal"
    if FED.search(f) and not NOT_FED.search(f):
        if _state_sig(f) and not STRONG_FED.search(f):
            return "State"
        return "Federal"
    if _state_sig(f) and not LOCAL.search(f):
        return "State"
    if LOCAL.search(f):
        return "Local"
    return "Institutional"


def sql_fed_state(col):
    """SQL (DuckDB) boolean expressions (fed_expr, state_expr) mirroring categorize(), on `col`."""
    u = f"upper(trim({col}))"
    exact = "('" + "','".join(s.replace("'", "''") for s in FED_EXACT) + "')"
    sig = (f"(regexp_matches({u}, '\\bSTATE OF\\b') OR regexp_matches({u}, 'COMMONWEALTH OF') "
           f"OR (regexp_matches({u}, '{P_STATENAME}') AND regexp_matches({u}, '\\bSTATE\\b')) "
           f"OR regexp_matches({u}, '{P_STATEAGENCY}'))")
    fed = (f"({u} IN {exact} OR (regexp_matches({u}, '{P_FED}') AND NOT regexp_matches({u}, '{P_NOTFED}') "
           f"AND NOT (({sig}) AND NOT regexp_matches({u}, '{P_STRONGFED}'))))")
    state = f"(({sig}) AND NOT regexp_matches({u}, '{P_LOCAL}'))"
    return fed, state


def report(min_parcels):
    con = duckdb.connect()
    con.execute("PRAGMA memory_limit='4GB'")
    con.execute("PRAGMA threads=2")
    con.execute("PRAGMA preserve_insertion_order=false")
    fam = con.execute(f"""select owner_family, sum(parcels) parcels from read_parquet('{SUMMARY}')
        where owner_family is not null group by 1""").df()
    fam["category"] = fam["owner_family"].map(categorize)
    inst = fam[(fam["category"] == "Institutional") & (fam["parcels"] >= min_parcels)].copy()
    inst[["owner_family", "category", "parcels"]].to_parquet(TARGET, index=False)
    print(f"institutional target (owner_family >= {min_parcels:,}): {len(inst):,} families, "
          f"{int(inst['parcels'].sum()):,} parcels -> {os.path.relpath(TARGET, ROOT)}")
    print("(Federal/State are name-based at extract time, not from this list.)")


def extract():
    import geopandas as gpd
    import pandas as pd
    raw = os.path.join(OWN, "_major_owners_raw.parquet")
    fed, state = sql_fed_state("p.owner_norm")
    con = duckdb.connect()
    con.execute("PRAGMA memory_limit='6GB'")
    con.execute("PRAGMA threads=3")
    con.execute("PRAGMA preserve_insertion_order=false")
    con.execute(f"PRAGMA temp_directory='{os.path.join(OWN, '_spill')}'")
    inst_sub = f"(select owner_family from read_parquet('{TARGET}') where category='Institutional')"
    cat = (f"case when {fed} then 'Federal' when {state} then 'State' "
           f"when p.owner_family in {inst_sub} then 'Institutional' end")
    print("extracting major-owner parcels (name-based fed/state, scan over all normalized)...", flush=True)
    con.execute(f"""copy (
        select geometry, owner_label, category, state, county_name, owner_norm, assessed_value from (
          select p.geometry, coalesce(p.owner_norm, p.owner_family) owner_label, {cat} category,
                 p.state, p.county_name, p.owner_norm, try_cast(p.assessed_value as bigint) assessed_value
          from read_parquet('{NORM}', union_by_name=true) p
        ) where category is not null
      ) to '{raw}' (format parquet)""")
    n = con.execute(f"select count(*) from read_parquet('{raw}')").fetchone()[0]
    con.close()
    print(f"  {n:,} major-owner parcels; converting to geoparquet...", flush=True)
    df = pd.read_parquet(raw)
    gdf = gpd.GeoDataFrame(df.drop(columns=["geometry"]),
                           geometry=gpd.GeoSeries.from_wkb(df["geometry"]), crs="EPSG:4326")
    gdf.to_parquet(OUT, index=False)
    os.remove(raw)
    print(f"  wrote {os.path.relpath(OUT, ROOT)} ({n:,}) by category: "
          f"{gdf.groupby('category').size().to_dict()}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--extract", action="store_true")
    ap.add_argument("--min", type=int, default=1000)
    a = ap.parse_args()
    if a.report:
        report(a.min)
    if a.extract:
        extract()
