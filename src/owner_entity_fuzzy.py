"""Layer-1.1 fuzzy NAME consolidation — an ADDITIVE overlay on owner_entity (v1).

v1 (owner_entity.py) collapses LLC/suffix variants but leaves brand families fragmented:
'D R HORTON' (32,694) and 'DR HORTON' (13,999) are separate; 'LENNAR HOMES OF TEXAS/INDIANA/
IDAHO' don't roll into 'LENNAR HOMES'. This module merges those SAFELY, name-only, two ways:

  PASS A  spacing-collapse  (provably safe): collapse runs of single-letter tokens, so
          'D R HORTON' -> 'DR HORTON'. No two distinct entities differ only by initial spacing.

  PASS B  brand-anchor + geo-suffix (precision-guarded): peel a trailing GEOGRAPHIC suffix
          (full US state name / directional macro-region / major city, optionally joined by a
          connector OF/AND/THE) ONLY when the remaining brand core keeps >= 2 tokens, then group
          entities sharing that core into a super-entity -- but only if the group has >= 2 distinct
          members summing to >= ANCHOR_MIN parcels. So 'DR HORTON TEXAS'+'DR HORTON INDIANA' ->
          'DR HORTON'; 'PULTE HOMES OF {TEXAS,OHIO,INDIANA}' -> 'PULTE HOMES'.

PRECISION GUARDS (why this won't repeat the rejected address-merge's false merges):
  * NO 2-letter state abbreviations (CO=Colorado vs CO=Company; IN/OR/OK are English words).
  * core must keep >= 2 tokens  => 'STATE OF MINNESOTA'->'STATE' REJECTED, 'LENNAR ARIZONA'->
    'LENNAR' REJECTED (both stay themselves). No single-token cores ever merge.
  * peel requires >= 1 real GEO token  => connector-only suffixes ('DELTONA THE') never merge.
  * must start with the FULL core  => 'CITY OF DELTONA' / 'THE DELTONA' never fold into 'DELTONA'.
  * institutional focus: only entities with >= CAND_MIN parcels are considered (the long tail,
    where individual-name false merges live, is left untouched -> self).

LOW BLAST RADIUS: v1 owner_entity.parquet is NOT modified (rail project + theses consume it).
Output is a small OVERLAY of only the entities that gained a non-self super; downstream applies
super_id = overlay.get(entity_id, entity_id).

Output (data/parcels/owners/):
  owner_entity_fuzzy.parquet       entity_id, entity_name, super_entity_id, super_entity_name, rule
                                   (ONLY rows where super != self -- the merges)
  super_entity_summary.parquet     super rollup: sub_entities, owner_variants, parcels, value, states
Audit: recon/owner_entity_fuzzy_audit.md  (every super + its members, for human review)
Usage: python src/owner_entity_fuzzy.py
"""
import hashlib
import os
import re

import duckdb
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OWN = os.path.join(ROOT, "data", "parcels", "owners")
SUMMARY = os.path.join(OWN, "owner_entity_summary.parquet")
OUT_MAP = os.path.join(OWN, "owner_entity_fuzzy.parquet")
OUT_SUM = os.path.join(OWN, "super_entity_summary.parquet")
AUDIT = os.path.join(ROOT, "recon", "owner_entity_fuzzy_audit.md")

CAND_MIN = 25        # only consolidate institutional entities (>=25 parcels)
ANCHOR_MIN = 1500    # a super must aggregate >= this many parcels (avoid tiny/spurious supers)

STATES = {
    "ALABAMA", "ALASKA", "ARIZONA", "ARKANSAS", "CALIFORNIA", "COLORADO", "CONNECTICUT",
    "DELAWARE", "FLORIDA", "GEORGIA", "HAWAII", "IDAHO", "ILLINOIS", "INDIANA", "IOWA",
    "KANSAS", "KENTUCKY", "LOUISIANA", "MAINE", "MARYLAND", "MASSACHUSETTS", "MICHIGAN",
    "MINNESOTA", "MISSISSIPPI", "MISSOURI", "MONTANA", "NEBRASKA", "NEVADA", "OHIO",
    "OKLAHOMA", "OREGON", "PENNSYLVANIA", "TENNESSEE", "TEXAS", "UTAH", "VERMONT",
    "VIRGINIA", "WASHINGTON", "WISCONSIN", "WYOMING",
    # multi-word states are handled token-wise below (NEW MEXICO etc.) via REGION extras
}
# directional macro-regions + a few unambiguous compound geos (NOT generic 'VALLEY/BAY/COAST'
# which are real name parts, e.g. 'VALLEY NATIONAL BANK')
REGIONS = {"MIDWEST", "NORTHWEST", "SOUTHWEST", "SOUTHEAST", "NORTHEAST", "CAROLINAS"}
# curated major US cities that show up as homebuilder-subsidiary suffixes
CITIES = {
    "ATLANTA", "AUSTIN", "BIRMINGHAM", "BOISE", "CHARLOTTE", "CHICAGO", "CINCINNATI",
    "CLEVELAND", "COLUMBUS", "DALLAS", "DENVER", "DETROIT", "HOUSTON", "INDIANAPOLIS",
    "JACKSONVILLE", "KANSAS CITY", "LASVEGAS", "MEMPHIS", "MIAMI", "NASHVILLE", "ORLANDO",
    "PHOENIX", "PORTLAND", "RALEIGH", "RICHMOND", "SACRAMENTO", "SANANTONIO", "SEATTLE",
    "TAMPA", "TUCSON", "TULSA",
}
GEO = STATES | REGIONS | CITIES
CONNECTOR = {"OF", "AND", "THE", "AT", "IN", "FOR"}  # allowed in suffix, but never count as GEO
# GOVERNMENT / jurisdiction tokens: for these the geo qualifier is IDENTIFYING (Douglas County GA
# is a DIFFERENT government than a generic 'Douglas County'), not a subsidiary brand -> never geo-
# merge them (spacing merges still apply). Trades a few correct merges (City of Akron Ohio) for
# eliminating the ambiguous-jurisdiction false-merge class. Precision over recall.
GOV_TOKENS = {"COUNTY", "CITY", "STATE", "STATES", "DEPARTMENT", "DEPT", "DISTRICT", "TOWNSHIP",
              "TWP", "BOROUGH", "PARISH", "TOWN", "VILLAGE", "AUTHORITY", "COMMISSION",
              "MUNICIPAL", "USA", "US", "FEDERAL", "BUREAU", "SCHOOL", "COLLEGE", "UNIVERSITY",
              "HOUSING", "REDEVELOPMENT", "DNR", "DOT"}

_LETTER_RUN = re.compile(r"\b([A-Z])(?:\s+([A-Z]))+\b")


def collapse_initials(name):
    """'D R HORTON' -> 'DR HORTON'; 'J P MORGAN CHASE' -> 'JP MORGAN CHASE'."""
    def _join(m):
        return m.group(0).replace(" ", "")
    return re.sub(r"\b(?:[A-Z]\s+){1,}[A-Z]\b", _join, name)


def geo_core(toks):
    """Peel a trailing geographic suffix. Return (core_tuple, peeled_list) or (toks, []) if no
    valid peel. Valid iff >=1 peeled token is a real GEO token and core keeps >=2 tokens.
    Government/jurisdiction entities are never geo-peeled (the geo word is identifying)."""
    if any(t in GOV_TOKENS for t in toks):
        return tuple(toks), []
    core = list(toks)
    peeled, any_geo = [], False
    while core and (core[-1] in GEO or core[-1] in CONNECTOR):
        t = core.pop()
        peeled.insert(0, t)
        if t in GEO:
            any_geo = True
    if any_geo and len(core) >= 2 and peeled:
        return tuple(core), peeled
    return tuple(toks), []


def sid(s):
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def build():
    con = duckdb.connect()
    con.execute("PRAGMA memory_limit='3GB'")
    con.execute("PRAGMA threads=2")
    df = con.execute(f"""select entity_name, entity_id, owner_variants, parcels, total_value,
        total_value_dedup, states
        from read_parquet('{SUMMARY}') where parcels >= {CAND_MIN} and entity_name is not null
        """).df()
    con.close()
    print(f"loaded {len(df):,} institutional entities (>= {CAND_MIN} parcels)")

    # PASS A: spacing-collapse -> spacing_key
    df["toks"] = df["entity_name"].map(lambda s: tuple(collapse_initials(s).split()))
    df["spacing_key"] = df["toks"].map(lambda t: " ".join(t))

    # PASS B: geo core per (already spacing-collapsed) token tuple
    cores = df["toks"].map(geo_core)
    df["core_key"] = [" ".join(c) for c, _ in cores]   # geo-stripped core (or self)

    # group key = the geo core (this unifies spacing variants too, since toks are collapsed)
    g = df.groupby("core_key")
    grp_parcels = g["parcels"].sum()
    grp_members = g["entity_name"].nunique()

    # a core_key becomes a SUPER iff >=2 distinct member names and combined parcels >= ANCHOR_MIN
    super_keys = set(grp_parcels[(grp_parcels >= ANCHOR_MIN) & (grp_members >= 2)].index)

    def super_of(row):
        return row["core_key"] if row["core_key"] in super_keys else None
    df["super_key"] = df.apply(super_of, axis=1)

    merged = df[df["super_key"].notna()].copy()
    # display name = highest-parcel original member; rule = spacing (core==own collapsed toks) or geo
    rep = (merged.sort_values("parcels", ascending=False)
                 .groupby("super_key").first()["entity_name"].to_dict())
    merged["super_entity_name"] = merged["super_key"].map(rep)
    merged["super_entity_id"] = merged["super_key"].map(lambda k: sid(k))
    merged["rule"] = [("spacing" if " ".join(t) == ck else "geo")
                      for t, ck in zip(merged["toks"], merged["core_key"])]
    # only keep rows that actually CHANGE (drop the member that already equals the super display
    # AND has the same name -- it's the anchor; keep it too so the overlay is self-contained)
    out = merged[["entity_id", "entity_name", "super_entity_id", "super_entity_name", "rule"]]
    out.to_parquet(OUT_MAP, index=False)

    # super rollup
    roll = (merged.groupby(["super_key", "super_entity_id", "super_entity_name"])
                  .agg(sub_entities=("entity_name", "nunique"),
                       owner_variants=("owner_variants", "sum"),
                       parcels=("parcels", "sum"),
                       total_value=("total_value", "sum"),
                       total_value_dedup=("total_value_dedup", "sum"),
                       states=("states", "max"))
                  .reset_index().sort_values("parcels", ascending=False))
    roll[["super_entity_name", "super_entity_id", "sub_entities", "owner_variants",
          "parcels", "total_value", "total_value_dedup", "states"]].to_parquet(OUT_SUM, index=False)

    # metrics
    n_super = roll.shape[0]
    n_subs = int(roll["sub_entities"].sum())
    parcels_consolidated = int(roll["parcels"].sum())
    print(f"formed {n_super:,} super-entities from {n_subs:,} institutional entities "
          f"({n_subs - n_super:,} entities folded away), covering {parcels_consolidated:,} parcels")
    n_geo = merged[merged["rule"] == "geo"]["super_key"].nunique()
    print(f"  supers involving a geo merge: {n_geo:,}  (rest are pure spacing merges)")

    # AUDIT: every super + members, top by parcels (this is the human-review artifact)
    with open(AUDIT, "w", encoding="utf-8") as f:
        f.write("# owner_entity_fuzzy audit — Layer-1.1 super-entities (REVIEW before downstream use)\n\n")
        f.write(f"CAND_MIN={CAND_MIN} parcels, ANCHOR_MIN={ANCHOR_MIN} parcels. "
                f"{n_super:,} supers / {n_subs:,} member entities. "
                f"v1 owner_entity.parquet UNCHANGED; this is an additive overlay.\n\n")
        f.write("Apply downstream as: super_id = overlay.get(entity_id, entity_id).\n\n")
        for _, r in roll.head(150).iterrows():
            mem = (merged[merged["super_key"] == r["super_key"]]
                   .sort_values("parcels", ascending=False))
            rules = set(mem["rule"])
            f.write(f"## {r['super_entity_name']}  —  {int(r['parcels']):,} parcels, "
                    f"{int(r['sub_entities'])} members  [{','.join(sorted(rules))}]\n")
            for _, m in mem.iterrows():
                f.write(f"  - {m['entity_name']}  ({int(m['parcels']):,})  [{m['rule']}]\n")
            f.write("\n")
    print(f"wrote {os.path.relpath(OUT_MAP, ROOT)}, {os.path.relpath(OUT_SUM, ROOT)}, "
          f"{os.path.relpath(AUDIT, ROOT)}")


if __name__ == "__main__":
    build()
