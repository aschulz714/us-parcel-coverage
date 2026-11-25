"""OWNER STANDARDIZATION (Tier 1) — one owner, one spelling, across every county.

Adds two columns to every normalized parquet (raw `owner` is NEVER modified):
  owner_norm    deterministic normalization: uppercase, punctuation stripped, whitespace
                collapsed, legal-suffix tokens standardized (L.L.C.->LLC, TRUSTEE->TTEE,
                AND->&), then the committed alias dictionary (data/owner_aliases.csv —
                every 'USA' variant becomes UNITED STATES OF AMERICA, etc.).
  owner_family  COMMON-OWNERSHIP rollup for organization-like names: distinct legal
                entities stay distinct in owner_norm, but WEYERHAEUSER NR CO and
                WEYERHAEUSER TIMBER HOLDINGS INC share family WEYERHAEUSER. Sources:
                curated prefix rules (data/owner_families.csv, with a source column) or
                the name stem after iteratively stripping structure tokens from the end.
                Individuals get NO family (surname rollups over-merge; that's Tier 2's
                corroborated-matching job).

Also writes data/parcels/owners/owner_summary.parquet: one row per (owner_norm) with
parcel count, state count, total value, and family — the national ownership index. Carries
two value columns: total_value (raw sum of cleaned assessed_value) and total_value_dedup
(stamped-total artifacts collapsed — use this for $-ranking; see docs/VALUE_QUALITY.md).

Usage:
  python src/owners_std.py --state WA      # one state
  python src/owners_std.py --all           # every state in the store
  python src/owners_std.py --summary-only  # just rebuild the summary table
"""
import argparse, csv, glob, os, re, sys

import duckdb
import geopandas as gpd
import pandas as pd

HERE = os.path.dirname(__file__)
ROOT = os.path.dirname(HERE)
NORM = os.path.join(ROOT, "data", "parcels", "normalized")
OWNERS_DIR = os.path.join(ROOT, "data", "parcels", "owners")
ALIASES_CSV = os.path.join(ROOT, "data", "owner_aliases.csv")
FAMILIES_CSV = os.path.join(ROOT, "data", "owner_families.csv")

# source-side placeholder strings that masquerade as owners (QA finding 2026-06-12:
# WI 'NOT AVAILABLE' x89k, TX 'UNKNOWN OWNER' x10k, ID 'PRIVATE' x4k) — owner_norm gets
# the bracketed sentinel so rankings exclude them; raw owner keeps the source string
PLACEHOLDER_SENTINELS = {
    "NOT AVAILABLE": "[NOT AVAILABLE - source placeholder]",
    "UNKNOWN OWNER": "[UNKNOWN OWNER - source placeholder]",
    "UNKNOWN": "[UNKNOWN OWNER - source placeholder]",
    "PRIVATE": "[PRIVATE - source placeholder]",
    "N A": "[NOT AVAILABLE - source placeholder]",
    "OWNER UNKNOWN": "[UNKNOWN OWNER - source placeholder]",
    "CURRENT OWNER": "[CURRENT OWNER - source placeholder]",
    "NOT PUBLISHED": "[NOT PUBLISHED - source placeholder]",       # CO Pueblo confidential-owner
    "OWNER NOT PUBLISHED": "[NOT PUBLISHED - source placeholder]",
    "SUPPRESSED": "[OWNER SUPPRESSED - source placeholder]",
    "OWNER SUPPRESSED": "[OWNER SUPPRESSED - source placeholder]",
    # found 2026-06-20 via INSIGHTS owner-ranking audit — blanket per-source placeholders that
    # masqueraded as top owners (whole-county counts, huge phantom value); exact-string matched.
    "UNAVAILABLE OWNER": "[NOT AVAILABLE - source placeholder]",                  # NYC (Kings/New York)
    "CURRENT OWNER CURRENT CO OWNER": "[CURRENT OWNER - source placeholder]",     # CT (Middlesex/New London)
    "EAGLE COUNTY PROPERTY OWNER": "[NOT AVAILABLE - source placeholder]",        # CO Eagle blanket
    # found 2026-09-25 via the owner-entity v2 review: San Diego's non-owner flag ranked #20
    # nationally (17,238 parcels) and Polk IA's right-of-way label (2,809). 'INFORMATION ONLY
    # <name>' keeps its real owner name, so only these exact forms are placeholders.
    "INFORMATION ONLY": "[INFORMATION ONLY - source placeholder]",
    "INFORMATION ONLY PARCEL": "[INFORMATION ONLY - source placeholder]",
    "INFORMATION ONLY CARD": "[INFORMATION ONLY - source placeholder]",
    "STREET": "[STREET - source placeholder]",
}
# pattern placeholders, applied after the exact ones: pointers to another parcel's record
# (Cuyahoga 'LISTED WITH 36123016', 'LISTED WITH 10126401 TO 403'; Benzie MI 'SEE 03 001 280 00
# FOR 2009', 'SEE 02 195 001 THRU 025 FOR') and names with no letters at all ('0' on 24k parcels,
# '&'). 'SEE' + digit cannot be the surname See.
PLACEHOLDER_PATTERNS = [
    (r"^LISTED WITH(?: |$)", "[LISTED WITH ANOTHER PARCEL - source placeholder]"),
    (r"^SEE [0-9].* FOR(?: |$)", "[SEE ANOTHER PARCEL - source placeholder]"),
    (r"^[^A-Z]+$", "[NO OWNER NAME - source placeholder]"),
]

# token-level standardization applied AFTER uppercase/punctuation cleanup
TOKEN_MAP = {
    "L L C": "LLC", "LLC": "LLC", "INCORPORATED": "INC", "CORPORATION": "CORP",
    "COMPANY": "CO", "LIMITED": "LTD", "TRUSTEE": "TTEE", "TRUSTEES": "TTEES",
    "TSTEE": "TTEE", "AND": "&", "ET UX": "ETUX", "ET AL": "ETAL", "ET VIR": "ETVIR",
    "REVOCABLE": "REV", "PARTNERSHIP": "PTNSHP",
}
# org detection + family stem stripping
ORG_TOKENS = {"LLC", "INC", "CORP", "CO", "LTD", "LP", "LLP", "LLLP", "PLLC", "PC",
              "COMPANY", "CORPORATION", "HOLDINGS", "PTNSHP", "PARTNERS", "PROPERTIES",
              "ENTERPRISES", "INVESTMENTS", "GROUP", "ASSOCIATION", "ASSN", "AUTHORITY",
              "DISTRICT", "CHURCH", "UNIVERSITY", "CITY", "COUNTY", "STATE", "BANK"}
STRIP_END_TOKENS = {"LLC", "INC", "CORP", "CO", "LTD", "LP", "LLP", "LLLP", "PLLC", "PC",
                    "HOLDINGS", "PTNSHP", "PARTNERS", "PROPERTIES", "ENTERPRISES", "GROUP"}

_PUNCT = re.compile(r"[^\w& ]+")
_WS = re.compile(r"\s+")

# --- stamped-total de-dup (docs/VALUE_QUALITY.md) ---
# A "stamped-total" artifact is an installation/portfolio TOTAL copied verbatim onto each of its N
# parcels (Georgia Power Plant Bowen $1.28B x77, Camp Lejeune $3.41B x26, Travis-Co TX BPP/rendition
# accounts xN). A naive sum(assessed_value) then over-counts that installation N-fold. We treat a
# value as stamped — and collapse it to a SINGLE instance in total_value_dedup — when it is large
# AND repeated on many parcels of the SAME owner in the SAME county. Thresholds were chosen from the
# national value distribution: $10M sits between p99 ($3.3M) and p99.9 ($46.7M), and >=5
# identical-to-the-dollar copies inside one owner+county is the artifact signature. Legitimate big
# owners hold DISTINCT large values (Disney's many ~$500M parcels appear as count<=3 groups), so
# they are untouched; the raw sum is kept alongside as total_value for transparency.
STAMP_MIN_VALUE = 10_000_000
STAMP_MIN_COUNT = 5


def load_config():
    aliases = {}
    with open(ALIASES_CSV, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            aliases[r["alias"].strip().upper()] = r["canonical"].strip().upper()
    families = []
    with open(FAMILIES_CSV, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            families.append((r["prefix"].strip().upper(), r["family"].strip().upper()))
    families.sort(key=lambda x: -len(x[0]))  # longest prefix wins
    return aliases, families


def normalize_series(s, aliases):
    """Vectorized Tier-1 normalization of an owner-name series."""
    out = s.fillna("").astype(str).str.upper()
    out = out.str.replace(_PUNCT, " ", regex=True).str.replace(_WS, " ", regex=True).str.strip()
    for tok, rep in TOKEN_MAP.items():
        out = out.str.replace(rf"\b{tok}\b", rep, regex=True)
    out = out.str.replace(_WS, " ", regex=True).str.strip()
    out = out.map(lambda v: PLACEHOLDER_SENTINELS.get(v) or aliases.get(v, v))
    for pat, sentinel in PLACEHOLDER_PATTERNS:
        out = out.mask(out.str.contains(pat, regex=True), sentinel)
    return out.replace("", None)


# Congregational names break the name->ownership inference: thousands of INDEPENDENT
# legal entities share names like FIRST BAPTIST CHURCH (446 counties), so a name-stem
# "family" would assert common ownership that does not exist. These names get family=None
# from the generic fallback. Curated prefix rules still win first — a real, evidence-based
# rollup (e.g. a specific diocese that does hold title centrally) can always be added.
CONGREGATION_TOKENS = {"CHURCH", "IGLESIA", "PARISH", "TEMPLE", "SYNAGOGUE", "MOSQUE",
                       "MASJID", "CONGREGATION", "MINISTRIES", "MINISTRY"}


def family_series(norm, families):
    """owner_family for org-like names; None for individuals and sentinels.
    SEMANTIC CONTRACT: owner_family asserts COMMON OWNERSHIP — curated rules are
    evidence-based; the generic stem fallback only unifies suffix variants of one
    name and must not bridge independent entities that happen to share a name."""
    def fam(v):
        if not v or v.startswith("["):
            return None
        for prefix, family in families:
            if v.startswith(prefix):
                return family
        toks = v.split()
        if not (set(toks) & ORG_TOKENS):
            return None  # individual: no rollup at Tier 1
        if set(toks) & CONGREGATION_TOKENS:
            return None  # same-name independent congregations: no common owner
        while toks and toks[-1] in STRIP_END_TOKENS:
            toks.pop()
        stem = " ".join(toks).strip()
        return stem or None
    return norm.map(fam)


def apply_files(files, aliases, families):
    import pyarrow.parquet as pq
    from parquet_cols import write_columns  # pyarrow column swap: geometry stays opaque bytes
    for p in files:
        table = pq.read_table(p)
        if "owner" not in table.column_names:
            continue
        owner = table.column("owner").to_pandas().astype(object)
        owner_norm = normalize_series(owner, aliases)
        # sentinels pass through unchanged so they stay bracketed/excludable
        sentinel = owner.fillna("").str.startswith("[")
        owner_norm = owner_norm.mask(sentinel, owner)
        owner_family = family_series(owner_norm, families)
        write_columns(p, table, {"owner_norm": owner_norm, "owner_family": owner_family})
        print(f"  {os.path.basename(p)}: norm {owner_norm.notna().mean():.0%}, "
              f"family {owner_family.notna().mean():.0%}", flush=True)


def apply_state(st2, aliases, families):
    apply_files(sorted(glob.glob(os.path.join(NORM, f"{st2}*.parquet"))), aliases, families)


def build_summary():
    os.makedirs(OWNERS_DIR, exist_ok=True)
    con = duckdb.connect()
    # bounded memory + disk spill: the ~90M-row group-by must coexist with other workloads
    spill = os.path.join(OWNERS_DIR, "_duckdb_spill").replace("\\", "/")
    con.execute(f"SET memory_limit='6GB'; SET temp_directory='{spill}'; "
                "SET threads=2; SET preserve_insertion_order=false")
    pat = os.path.join(NORM, "*.parquet").replace("\\", "/")
    out = os.path.join(OWNERS_DIR, "owner_summary.parquet").replace("\\", "/")
    base = f"read_parquet('{pat}', union_by_name=true)"
    keep = "owner_norm IS NOT NULL AND owner_norm NOT LIKE '[%'"
    # Step 1 (materialized between steps -> bounds peak memory): per-owner stamped-total EXCESS.
    # Pre-filtering to large values first keeps only a tiny slice feeding this group-by; collapsing
    # each (owner, county, value) artifact group to one instance means the excess to drop = (c-1)*v.
    con.execute(f"""
        CREATE TEMP TABLE _stamp_excess AS
        SELECT owner_norm, sum((c - 1) * av) AS excess FROM (
            SELECT owner_norm, county_fips, CAST(assessed_value AS DOUBLE) AS av, count(*) AS c
            FROM {base}
            WHERE {keep} AND CAST(assessed_value AS DOUBLE) >= {STAMP_MIN_VALUE}
            GROUP BY owner_norm, county_fips, CAST(assessed_value AS DOUBLE)
            HAVING count(*) >= {STAMP_MIN_COUNT}
        ) GROUP BY owner_norm""")
    # Step 2: per-owner rollup over the cleaned values + de-duped value = naive minus stamped excess.
    con.execute(f"""
        COPY (
          SELECT b.owner_norm, b.owner_family, b.parcels, b.counties, b.states, b.total_value,
                 b.total_value - coalesce(e.excess, 0) AS total_value_dedup
          FROM (
            SELECT owner_norm, any_value(owner_family) AS owner_family,
                   count(*) AS parcels, count(DISTINCT county_fips) AS counties,
                   count(DISTINCT state) AS states, sum(assessed_value) AS total_value
            FROM {base} WHERE {keep}
            GROUP BY owner_norm
          ) b LEFT JOIN _stamp_excess e USING (owner_norm)
        ) TO '{out}' (FORMAT PARQUET, COMPRESSION ZSTD)""")
    n, cut = con.execute(f"""SELECT count(*),
        coalesce(sum(total_value),0) - coalesce(sum(total_value_dedup),0)
        FROM read_parquet('{out}')""").fetchone()
    print(f"owner summary: {n:,} distinct owner_norm values -> {out}")
    print(f"  stamped-total de-dup removed ${cut/1e9:,.1f}B of double-counted value "
          f"(total_value kept; total_value_dedup added)")


def build_summary_partitioned():
    """State-partitioned owner_summary rebuild for disk-constrained boxes. The single-pass
    build_summary() spills >14GB on 122M rows (killed the 2026-07-11 overnight at an 11GB
    spill with 4GB free). Per-state group-bys spill ~1-2GB worst case (TX), and every
    column merges EXACTLY across states: county_fips is state-prefixed (distinct-county
    counts add), the stamped-total excess is (owner, county, value)-local (state-local by
    construction), and `states` = one partial row per state. Same output schema/semantics
    as build_summary()."""
    import csv as _csv
    import shutil as _shutil
    os.makedirs(OWNERS_DIR, exist_ok=True)
    part_dir = os.path.join(OWNERS_DIR, "_summary_parts")
    _shutil.rmtree(part_dir, ignore_errors=True)
    os.makedirs(part_dir)
    with open(os.path.join(HERE, "county_methods.csv"), encoding="utf-8") as f:
        prefixes = sorted({r["fips"][:2] for r in _csv.DictReader(f)})
    keep = "owner_norm IS NOT NULL AND owner_norm NOT LIKE '[%'"
    spill = os.path.join(OWNERS_DIR, "_duckdb_spill").replace("\\", "/")
    for pre in prefixes:
        files = glob.glob(os.path.join(NORM, f"{pre}*.parquet"))
        if not files:
            continue
        con = duckdb.connect()
        con.execute(f"SET memory_limit='3GB'; SET temp_directory='{spill}'; "
                    "SET threads=2; SET preserve_insertion_order=false")
        pat = os.path.join(NORM, f"{pre}*.parquet").replace("\\", "/")
        base = f"read_parquet('{pat}', union_by_name=true)"
        part = os.path.join(part_dir, f"{pre}.parquet").replace("\\", "/")
        con.execute(f"""
            CREATE TEMP TABLE _stamp_excess AS
            SELECT owner_norm, sum((c - 1) * av) AS excess FROM (
                SELECT owner_norm, county_fips, CAST(assessed_value AS DOUBLE) AS av, count(*) AS c
                FROM {base}
                WHERE {keep} AND CAST(assessed_value AS DOUBLE) >= {STAMP_MIN_VALUE}
                GROUP BY owner_norm, county_fips, CAST(assessed_value AS DOUBLE)
                HAVING count(*) >= {STAMP_MIN_COUNT}
            ) GROUP BY owner_norm""")
        con.execute(f"""
            COPY (
              SELECT b.owner_norm, b.owner_family, b.parcels, b.counties, b.total_value,
                     b.total_value - coalesce(e.excess, 0) AS total_value_dedup
              FROM (
                SELECT owner_norm, any_value(owner_family) AS owner_family,
                       count(*) AS parcels, count(DISTINCT county_fips) AS counties,
                       sum(assessed_value) AS total_value
                FROM {base} WHERE {keep}
                GROUP BY owner_norm
              ) b LEFT JOIN _stamp_excess e USING (owner_norm)
            ) TO '{part}' (FORMAT PARQUET, COMPRESSION ZSTD)""")
        n = con.execute(f"SELECT count(*) FROM read_parquet('{part}')").fetchone()[0]
        con.close()
        print(f"  part {pre}: {n:,} owners", flush=True)
    # merge: compact rows only (one per owner per state)
    con = duckdb.connect()
    con.execute(f"SET memory_limit='3GB'; SET temp_directory='{spill}'; "
                "SET threads=2; SET preserve_insertion_order=false")
    parts = os.path.join(part_dir, "*.parquet").replace("\\", "/")
    out = os.path.join(OWNERS_DIR, "owner_summary.parquet").replace("\\", "/")
    con.execute(f"""
        COPY (
          SELECT owner_norm, any_value(owner_family) AS owner_family, sum(parcels) AS parcels,
                 sum(counties) AS counties, count(*) AS states, sum(total_value) AS total_value,
                 sum(total_value_dedup) AS total_value_dedup
          FROM read_parquet('{parts}')
          GROUP BY owner_norm
        ) TO '{out}' (FORMAT PARQUET, COMPRESSION ZSTD)""")
    n, cut = con.execute(f"""SELECT count(*),
        coalesce(sum(total_value),0) - coalesce(sum(total_value_dedup),0)
        FROM read_parquet('{out}')""").fetchone()
    con.close()
    _shutil.rmtree(part_dir, ignore_errors=True)
    _shutil.rmtree(spill.replace("/", os.sep), ignore_errors=True)
    print(f"owner summary (partitioned): {n:,} distinct owner_norm values -> {out}")
    print(f"  stamped-total de-dup removed ${cut/1e9:,.1f}B of double-counted value")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--state")
    ap.add_argument("--fips", nargs="*", help="restrict to specific county fips (cheap targeted re-norm)")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--summary-only", action="store_true")
    ap.add_argument("--partitioned", action="store_true",
                    help="state-partitioned summary build (bounded ~2GB spill; for disk-tight boxes)")
    ap.add_argument("--no-summary", action="store_true",
                    help="skip the (heavy) owner_summary rebuild; run --summary-only later to refresh it")
    args = ap.parse_args()
    if not args.summary_only:
        aliases, families = load_config()
        with open(os.path.join(HERE, "county_methods.csv"), encoding="utf-8") as f:
            st_to_2 = {r["state"]: r["fips"][:2] for r in csv.DictReader(f)}
        if args.fips:
            files = [g for f in args.fips
                     for g in glob.glob(os.path.join(NORM, f"{f}*.parquet"))]
            apply_files(files, aliases, families)
        elif args.state or args.all:
            if args.state:
                targets = [st_to_2[args.state.upper()]]
            else:
                targets = sorted({os.path.basename(p)[:2]
                                  for p in glob.glob(os.path.join(NORM, "*.parquet"))})
            for st2 in targets:
                apply_state(st2, aliases, families)
        else:
            sys.exit("--state XX, --fips ..., --all, or --summary-only")
    if not args.no_summary:
        build_summary_partitioned() if args.partitioned else build_summary()


if __name__ == "__main__":
    main()
