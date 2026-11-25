"""Owner-footprint lookup — query any owner, get its national portfolio.

Build the index once (one scan over normalized), then queries are instant:
  python src/owner_lookup.py --build
  python src/owner_lookup.py --name "WEYERHAEUSER"
  python src/owner_lookup.py --name "PROGRESS RESIDENTIAL" --top 15

Index = owner_family x state x county_fips -> parcels/acres/value (orgs only; individuals excluded
since common personal names conflate). Substring-matches owner_family, so name variants are caught
automatically and shown so you can refine. Acreage filtered to sane 0..5M/parcel; within npc's
covered counties.
"""
import argparse
import csv
import os
import sys

import duckdb

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import major_owners as mo  # noqa: E402  (categorize)

NORM = os.path.join(ROOT, "data", "parcels", "normalized", "*.parquet").replace(os.sep, "/")
IDX = os.path.join(ROOT, "data", "parcels", "owners", "owner_footprint_index.parquet").replace(os.sep, "/")
METHODS = os.path.join(ROOT, "src", "county_methods.csv")


def _con(mem="3GB"):
    c = duckdb.connect()
    c.execute(f"PRAGMA memory_limit='{mem}'")
    c.execute("PRAGMA threads=2")
    c.execute("PRAGMA preserve_insertion_order=false")
    sp = os.path.join(ROOT, "data", "parcels", "owners", "_spill")
    os.makedirs(sp, exist_ok=True)
    c.execute(f"PRAGMA temp_directory='{sp}'")
    return c


def build():
    c = _con("4GB")
    print("building owner-footprint index (one scan over normalized)...", flush=True)
    c.execute(f"""copy (
        select owner_family, state, county_fips, count(*) parcels,
            sum(case when try_cast(acreage as double) between 0 and 5e6 then try_cast(acreage as double) end) acres,
            sum(try_cast(assessed_value as bigint)) val
        from read_parquet('{NORM}', union_by_name=true)
        where owner_family is not null group by 1, 2, 3
      ) to '{IDX}' (format parquet)""")
    n = c.execute(f"select count(*) from read_parquet('{IDX}')").fetchone()[0]
    print(f"wrote {IDX} ({n:,} owner x county rows)")


def lookup(name, top, loose=False):
    cn = {r["fips"]: r["county"] for r in csv.DictReader(open(METHODS, encoding="utf-8"))}
    c = _con()
    q = name.upper().strip()
    m = c.execute(f"""select owner_family, state, county_fips, parcels, acres, val
        from read_parquet('{IDX}') where upper(owner_family) like '%{q.replace("'", "''")}%'""").df()
    if not len(m):
        print(f"no owner_family matching '{name}'. (Index is org-only; try a shorter/different fragment.)")
        return
    m["acres"] = m["acres"].fillna(0)
    m["val"] = m["val"].fillna(0)
    m["fam_u"] = m["owner_family"].str.upper().str.strip()
    m = m[~m["fam_u"].map(mo.is_placeholder)]                 # drop placeholder/non-owner values
    # PRIMARY = query is the LEADING token of the name (the actual owner); INCIDENTAL = embedded
    # (the town 'School District of X', an address, a person, a 'C O X' care-of) -> excluded.
    m["primary"] = m["fam_u"].str.startswith(q)
    inc = m[~m["primary"]]
    n_inc = inc["owner_family"].nunique()
    use = m if loose else m[m["primary"]]
    if not len(use):
        print(f"no owner whose name STARTS WITH '{name}' ({n_inc} incidental matches only). "
              f"--loose to include them.")
        return
    tot_p, tot_a, tot_v = int(use["parcels"].sum()), int(use["acres"].sum()), use["val"].sum()
    variants = (use.groupby("owner_family").agg(parcels=("parcels", "sum"), acres=("acres", "sum"))
                 .reset_index().sort_values("parcels", ascending=False))
    cat = mo.categorize(variants.iloc[0]["owner_family"]) or "Institutional"
    excl = "" if (loose or not n_inc) else f"  (+{n_inc} incidental excluded)"
    print(f"\n=== OWNER FOOTPRINT: '{name}'  ->  {len(variants)} name-variant(s){excl} ===")
    print(f"category (best-guess): {cat}")
    print(f"TOTAL: {tot_p:,} parcels | {tot_a:,} acres | ${tot_v/1e9:.2f}B assessed | "
          f"{use['state'].nunique()} states | {use['county_fips'].nunique()} counties")
    print("\ntop name-variants:")
    for _, r in variants.head(top).iterrows():
        print(f"   {r['owner_family'][:46]:46} {int(r['parcels']):>9,} parcels  {int(r['acres']):>12,} ac")
    if n_inc and not loose:
        ex = inc.groupby("owner_family")["parcels"].sum().sort_values(ascending=False).head(3).index
        print("   [excluded incidental: " + "; ".join(s[:34] for s in ex) + " ... --loose to include]")
    bystate = (use.groupby("state").agg(parcels=("parcels", "sum"), acres=("acres", "sum"))
                .reset_index().sort_values("acres", ascending=False))
    print("\ntop states (by acres):")
    for _, r in bystate.head(top).iterrows():
        print(f"   {r['state']}  {int(r['acres']):>12,} ac  {int(r['parcels']):>9,} parcels")
    bycty = (use.groupby(["county_fips", "state"]).agg(parcels=("parcels", "sum"), acres=("acres", "sum"))
              .reset_index().sort_values("parcels", ascending=False))
    bycty["county"] = bycty["county_fips"].map(cn).fillna(bycty["county_fips"])
    print("\ntop counties (by parcels):")
    for _, r in bycty.head(top).iterrows():
        print(f"   {r['county'][:24]:24} {r['state']}  {int(r['parcels']):>8,} parcels  {int(r['acres']):>10,} ac")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", action="store_true")
    ap.add_argument("--name")
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--loose", action="store_true", help="include incidental (embedded) name matches")
    a = ap.parse_args()
    if a.build:
        build()
    if a.name:
        lookup(a.name, a.top, a.loose)
