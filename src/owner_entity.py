"""Layer-1 owner entity resolution (name-based) — collapse owner_norm name/LLC variants
into resolved ENTITIES, so a fragmented owner (Progress Residential = ~1,900 LLC shells)
becomes one row.

Signal: name normalization with PRECISION GUARDS (v2, 2026-09-25).
  name_core = strip corporate suffixes (LLC/INC/CORP/LP...) + shell tokens (SERIES/BORROWER/REO)
  + SERIES numbers -> 'PROGRESS RESIDENTIAL BORROWER 2019-1 LLC' => 'PROGRESS RESIDENTIAL'.
  IDENTITY numbers are never stripped (v1 stripped every bare number, which merged 53k unrelated
  owner groups -- '007 REALTY LLC' + '01150 REALTY LLC' => 'REALTY', ranked #228 nationally):
    - a LEADING numeric run is an address or registry number ('123 MAIN STREET LLC',
      '9123-4567 QUEBEC INC');
    - a number right after an ID designator names a different entity ('HARRIS COUNTY MUD NO
      501', 'SCHOOL DISTRICT 60', '... TITLE AGENCY TR 60568', 'AMERICAN LEGION POST 12',
      'ROUTE 20 LLC', 'SMITH FAMILY TRUST 12 15 2005').
  GUARD A: the core must keep >=5 letters ('11300 IH 35 LLC' must not become 'IH').
  GUARD B: when series numbers were stripped, the core must keep one distinctive token -- a
  kept number, or a word outside the generic vocabulary. 'REALTY 12 LLC', 'CEMETERY 10' and
  'NORTH 40 LLC' keep their full names: the number was the only thing naming them. Suffix-only
  variants of an all-generic name still merge ('PUBLIC STORAGE INC' + 'PUBLIC STORAGE').
  A core failing a guard falls back to the exact full name. Names with no letters ('0', '&')
  are placeholders, not owners, and get no entity. Precision over recall.
  KNOWN LIMIT: identical names are not identical owners. owner_norm is national, so 'SMITH
  FAMILY TRUST' in Ohio and in Texas already share one owner_summary row; this layer then also
  joins 'SMITH FAMILY LLC'. Only corroboration (mailing address, SoS officers) separates them.

Deliberately NAME-ONLY in v1. A mailing-address tier was prototyped and REJECTED: naive
"shared address = same entity" false-merges unrelated individuals into institutions (e.g.
BROWN LILIETH, MALUCCI TERRI merged into 'DNR' because their tax c/o is the Michigan DNR's
address). A safe address tier needs name-compatibility logic -> v1.1. Layer 2 adds Secretary-
of-State / OpenCorporates registered-agent -> beneficial owner.

Output (data/parcels/owners/, or --outdir for a staged build):
  owner_entity.parquet          owner_norm -> entity_id, entity_name
  owner_entity_summary.parquet  entity rollup: owner_variants, parcels, total_value,
                                total_value_dedup (stamped-totals collapsed -- $-ranking), states
Usage: python src/owner_entity.py [--from-summary] [--outdir DIR]
"""
import os

import duckdb

from owners_std import STAMP_MIN_COUNT, STAMP_MIN_VALUE  # stamped-total de-dup thresholds

HERE = os.path.dirname(__file__)
ROOT = os.path.dirname(HERE)
NORM = os.path.join(ROOT, "data", "parcels", "normalized", "*.parquet")
OUTDIR = os.path.join(ROOT, "data", "parcels", "owners")
SUMMARY = os.path.join(OUTDIR, "owner_summary.parquet")  # source for the RAM-light --from-summary path

# whole-word corporate-form + shell tokens to strip (conservative: only unambiguous corporate
# forms; NOT 'CO'/'TR'/'PROPERTIES'/'HOLDINGS' which can be real name parts)
SUFFIX = (r"LLC|L L C|INC|INCORPORATED|CORP|CORPORATION|LP|L P|LLP|LTD|LIMITED|"
          r"SERIES|BORROWER|REO|SPE|SUB|TRUST")

# A number right after one of these tokens identifies a DIFFERENT entity: roads, organization
# units, special districts, land-trust / custodial-account numbers, trust dates. (Tokens as they
# appear in owner_norm: owners_std maps TRUSTEE -> TTEE.)
DESIGNATORS = (
    "ROUTE", "RT", "RTE", "HWY", "HIGHWAY", "INTERSTATE", "I", "IH", "US", "SR", "SH", "FM", "RM",
    "CR", "RR", "ROAD", "RD", "SPUR", "LOOP",
    "POST", "LODGE", "LOCAL", "CHAPTER", "TROOP", "AERIE",
    "DISTRICT", "DIST", "SD", "ISD", "CISD", "USD", "CSD", "UFSD", "MUD", "WCID", "FWSD", "UD",
    "PUD", "SUD", "ESD", "WSD", "LID", "RID", "WARD", "PRECINCT", "PCT",
    "NO", "NUM", "NUMBER", "NBR",
    "TR", "TRS", "TRUST", "TRST", "TTEE", "TTEES", "TRUSTEE", "CUSTODIAN", "CUST", "ACCT",
    "ACCOUNT", "IRA", "DTD", "DATED", "UAD", "UTD", "UDT",
    # custodial / land-trust account numbers ('... CUSTODIAN FBO 53603 IRA', 'CTLTC 000000126479'
    # = Chicago Title Land Trust Co + trust number), added 2026-09-25
    "FBO", "CTLTC",
)

# Words that identify no one on their own. A core made only of these (plus single letters) is
# not a name -- 'PROPERTIES', 'REAL ESTATE', 'MAIN STREET', 'NORTH', 'STREET ON BOOK PAGE'.
GENERIC = frozenset("""
    REALTY REALTORS REAL ESTATE ESTATES PROPERTY PROPERTIES PROP PROPS PPTY PPTIES HOLDING
    HOLDINGS HLDGS HLDG INVESTMENT INVESTMENTS INVESTORS INVESTOR INVEST INVS INV PARTNER
    PARTNERS PTNSHP PTNRS PTRS ASSOCIATES ASSOC ASSOCS ASSOCIATION ASSN GROUP GRP ENTERPRISE
    ENTERPRISES ENT VENTURE VENTURES DEVELOPMENT DEVELOPMENTS DEVELOPER DEVELOPERS DEV DEVEL
    MANAGEMENT MGMT MGT MANAGER MANAGERS CAPITAL EQUITY EQUITIES ASSET ASSETS FUND FUNDS
    ACQUISITION ACQUISITIONS RENTAL RENTALS LEASING LAND LANDS HOME HOMES HOUSING HOUSE
    RESIDENTIAL RESIDENCES COMMERCIAL INDUSTRIAL RETAIL OFFICE OFFICES BUILDING BUILDINGS BLDG
    APARTMENT APARTMENTS APTS APT OWNER OWNERS CO COMPANIES SERVICE SERVICES SOLUTIONS
    RESOURCES INTERNATIONAL INTL NATIONAL FINANCIAL FINANCE TRADING INDUSTRIES CONSTRUCTION
    BUILDERS CONSULTING SYSTEMS PROJECT PROJECTS FAMILY LIVING REV IRREVOCABLE TTEE TTEES TR
    TRS TRUSTEE TRUSTEES ETAL ETUX ETVIR JR SR II III IV
    THE OF AT ON IN FOR BY TO AN AND
    NORTH SOUTH EAST WEST NORTHEAST NORTHWEST SOUTHEAST SOUTHWEST NE NW SE SW CENTRAL UPPER
    LOWER NEW OLD
    STREET STREETS ST AVENUE AVE AV ROAD RD DRIVE DR LANE LN BOULEVARD BLVD WAY CIRCLE CIR
    TERRACE TER PARKWAY PKWY HIGHWAY HWY ROUTE RT RTE PIKE TRAIL TRL SQUARE SQ PLACE PL COURT
    CT PLAZA CENTER CENTRE CTR TOWER TOWERS PARK COMMONS VILLAGE VILLAGES MANOR BROADWAY MAIN
    ALLEY ROW WALK CROSSING POINT POINTE HEIGHTS
    STATE COUNTY CITY TOWN TOWNSHIP TWP BOROUGH PARISH DISTRICT DIST AUTHORITY BOARD
    DEPARTMENT DEPT COMMISSION PUBLIC SCHOOL SCHOOLS EDUCATION CEMETERY CHURCH
    BOOK PAGE PG DEED DB INSTRUMENT INST DOC PLAT LOT LOTS BLOCK BLK SECTION SEC RANGE RNG
    TRACT PARCEL PARCELS UNIT UNITS PHASE ACRES ACRE AC SUBDIVISION SUBD ADDITION
    UNKNOWN NONE NA SAME VACANT TAXPAYER CURRENT OCCUPANT RESIDENT NAME
    FARM FARMS RANCH STORAGE SELF MINI MOBILE TRAILER CONDO CONDOMINIUM CONDOMINIUMS
    TOWNHOMES TOWNHOUSE HOTEL MOTEL INN MARINA GOLF CLUB COUNTRY
""".split())

# Rows that are owners at all: bracketed '[...]' source placeholders and names with no letters
# ('0' x24k parcels, '&') get no entity.
KEEP_OWNER = ("owner_norm is not null and owner_norm <> '' and owner_norm not like '[%' "
              "and regexp_matches(owner_norm, '[A-Za-z]')")

_DESIG = "|".join(DESIGNATORS)
_GENERIC = "|".join(sorted(GENERIC))
_MACROS = [
    # exact full name (the fallback display name; byte-identical to v1's, so entity ids of
    # unchanged entities are stable)
    r"""create or replace macro oe_full(n) as trim(regexp_replace(regexp_replace(
          upper(coalesce(n, '')), '[^A-Z0-9 ]', ' ', 'g'), ' +', ' ', 'g'))""",
    # input to the core pipeline: like oe_full, but '&' survives as a '#' barrier token so the
    # spaced suffixes 'L P' / 'L L C' cannot match initials joined by '&' ('THOMPSON JERRY L & P
    # L', 'L & P ACRES LLC'). owner_norm never contains '#' (owners_std strips it).
    r"""create or replace macro oe_prep(n) as trim(regexp_replace(regexp_replace(regexp_replace(
          upper(coalesce(n, '')), '&', ' # ', 'g'), '[^A-Z0-9 #]', ' ', 'g'), ' +', ' ', 'g'))""",
    # mark identity numbers by gluing them with '_' so the series-number strip cannot touch
    # them: the leading numeric run ('_31_43_81 STREET'), then a designator + its number
    # ('NO_501', 'TRUST_12_15_2005'); three chain passes join up to four numbers.
    rf"""create or replace macro oe_protect(s) as
        regexp_replace(regexp_replace(regexp_replace(regexp_replace(regexp_replace(s,
          '^([0-9]+)\b', '_\1'),
          '(^| )({_DESIG}) ([0-9]+)\b', '\1\2_\3', 'g'),
          '(_[0-9]+) ([0-9]+)\b', '\1_\2', 'g'),
          '(_[0-9]+) ([0-9]+)\b', '\1_\2', 'g'),
          '(_[0-9]+) ([0-9]+)\b', '\1_\2', 'g')""",
    # strip corporate/shell tokens, then the remaining bare (series) numbers; drop the '&'
    # barriers and unglue identity numbers back into plain tokens
    rf"""create or replace macro oe_core(n) as
        trim(replace(regexp_replace(replace(regexp_replace(regexp_replace(regexp_replace(
          oe_protect(oe_prep(n)),
          '(^| )({SUFFIX})( |$)', ' ', 'g'),
          '(^| )({SUFFIX})( |$)', ' ', 'g'),
          '(^| )[0-9]+( [0-9]+)*( |$)', ' ', 'g'),
          '#', ' '), ' +', ' ', 'g'), '_', ' '))""",
    # does the core pipeline delete a (series) number from this name?
    r"""create or replace macro oe_strips_number(n) as
        regexp_matches(oe_protect(oe_prep(n)), '(^| )[0-9]+( |$)')""",
    # GUARD B: something is left after removing generic words and single letters (spaces are
    # doubled so one pass removes adjacent tokens)
    rf"""create or replace macro oe_distinctive(c) as
        trim(regexp_replace(' ' || replace(c, ' ', '  ') || ' ',
          ' ({_GENERIC}|[A-Z]) ', ' ', 'g')) <> ''""",
    r"""create or replace macro entity_name(n) as
        case when length(regexp_replace(oe_core(n), '[^A-Z]', '', 'g')) >= 5
                  and (oe_distinctive(oe_core(n)) or not oe_strips_number(n))
             then oe_core(n) else oe_full(n) end""",
]
NAME_CORE = "entity_name(owner_norm)"


def register_macros(con):
    """Define the entity_name() SQL macro (and its helpers) on a DuckDB connection."""
    for sql in _MACROS:
        con.execute(sql)


def build_from_summary(buckets=8, outdir=OUTDIR):
    """RAM-light entity rollup: aggregate the (compact) owner_summary up to the entity grain instead
    of re-scanning ~102M parcels. Used on the memory-constrained box where the parcel-scan build()
    crosses the harness low-RAM kill floor. Writes both outputs (map + summary). Trade-offs vs build():
      - owner_variants / parcels / total_value / total_value_dedup are EXACT (owner_summary is one
        row per owner_norm; total_value_dedup here is the owner-grain de-dup summed to the entity --
        same de-dup, missing only the rare cross-spelling same-county-same-value collapse).
      - `states` becomes a LOWER BOUND (max over the entity's owners): owner_summary stores a per-
        owner state COUNT, not the state set, so an exact entity distinct-state needs the parcel scan.
      - the map covers the owner_norms in owner_summary, so it is as current as owner_summary.
    Peak memory is bounded by chunking the GROUP BY into hash buckets (each ~1/buckets of entities),
    after precomputing entity_name once in a streaming pass (no hash table) so the chunks carry no
    regex cost. Requires owner_summary.parquet to already carry total_value_dedup."""
    os.makedirs(outdir, exist_ok=True)
    spill = os.path.join(outdir, "_duckdb_spill")
    os.makedirs(spill, exist_ok=True)
    prep = os.path.join(outdir, "_es_prep.parquet").replace("\\", "/")
    map_path = os.path.join(outdir, "owner_entity.parquet").replace("\\", "/")
    sum_path = os.path.join(outdir, "owner_entity_summary.parquet").replace("\\", "/")
    summ = SUMMARY.replace("\\", "/")
    con = duckdb.connect()
    con.execute("PRAGMA memory_limit='3GB'")  # chunks are small; 3G leaves headroom under the floor
    con.execute(f"PRAGMA temp_directory='{spill}'")
    con.execute("PRAGMA preserve_insertion_order=false")
    con.execute("PRAGMA threads=2")
    register_macros(con)

    # Step A -- streaming projection (no GROUP BY -> no hash table -> tiny memory): compute
    # entity_name once and persist (owner_norm spellings keep their precomputed core).
    con.execute(f"""copy (
        select owner_norm, {NAME_CORE} as entity_name, parcels, total_value, total_value_dedup,
               states
        from read_parquet('{summ}')
        where {KEEP_OWNER}
      ) to '{prep}' (format parquet, compression zstd)""")

    # Map -- owner_norm -> entity, straight from the prep (streaming, no regex).
    con.execute(f"""copy (
        select owner_norm, md5(entity_name) entity_id, entity_name
        from read_parquet('{prep}') where entity_name <> ''
      ) to '{map_path}' (format parquet, compression zstd)""")

    # Step B -- aggregate to entity grain in hash buckets (bounded peak memory, no regex now).
    parts = []
    for b in range(buckets):
        part = os.path.join(outdir, f"_es_part{b}.parquet").replace("\\", "/")
        con.execute(f"""copy (
            select entity_name, md5(entity_name) entity_id, count(*) owner_variants,
                   sum(parcels) parcels, sum(total_value) total_value,
                   sum(total_value_dedup) total_value_dedup, max(states) states
            from read_parquet('{prep}')
            where entity_name <> '' and (hash(entity_name) % {buckets}) = {b}
            group by entity_name
          ) to '{part}' (format parquet, compression zstd)""")
        parts.append(part)

    # Step C -- concatenate the bucket partials into the final summary (streaming).
    glob_parts = os.path.join(outdir, "_es_part*.parquet").replace("\\", "/")
    con.execute(f"copy (select * from read_parquet('{glob_parts}')) "
                f"to '{sum_path}' (format parquet, compression zstd)")
    n_own = con.execute(f"select count(*) from read_parquet('{map_path}')").fetchone()[0]
    n_ent = con.execute(f"select count(*) from read_parquet('{sum_path}')").fetchone()[0]
    cut = con.execute(f"""select coalesce(sum(total_value),0)-coalesce(sum(total_value_dedup),0)
        from read_parquet('{sum_path}')""").fetchone()[0]
    con.close()
    for p in parts + [prep]:
        try:
            os.remove(p)
        except OSError:
            pass
    print(f"owner_entity (from owner_summary, {buckets} buckets): {n_own:,} owner_norms -> "
          f"{n_ent:,} entities; stamped-total de-dup removed ${cut/1e9:,.1f}B. "
          f"NOTE: states is a lower bound on this path. Wrote {outdir}")


def build(reuse_map=False, outdir=OUTDIR):
    os.makedirs(outdir, exist_ok=True)
    spill = os.path.join(outdir, "_duckdb_spill")
    os.makedirs(spill, exist_ok=True)
    con = duckdb.connect()
    # 6GB matches the owner_summary build that succeeds on this 16.8G box. Counter-intuitively a
    # LOWER limit (4GB) was worse: it forces far more spill, and on Windows those temp files sit in
    # standby/file-cache memory that counts against the commit limit -> the box gets killed. The
    # rollup below is join-free (single streaming GROUP BY), so it has the same memory profile as
    # owner_summary -- no extra hash-join build side.
    con.execute("PRAGMA memory_limit='6GB'")
    con.execute(f"PRAGMA temp_directory='{spill}'")
    con.execute("PRAGMA preserve_insertion_order=false")
    con.execute("PRAGMA threads=2")
    register_macros(con)
    base = f"read_parquet('{NORM}', union_by_name=true)"
    notsent = KEEP_OWNER
    map_path = os.path.join(outdir, "owner_entity.parquet")
    sum_path = os.path.join(outdir, "owner_entity_summary.parquet")

    # owner_norm -> entity (guarded name_core), once per distinct owner_norm; stream to parquet.
    # --reuse-map skips this (the map is a deterministic function of owner_norm, so when only the
    # value rollup changed it can be reused) -- saves the multi-minute distinct-scan + map rewrite.
    if reuse_map and os.path.exists(map_path):
        print(f"reusing existing {os.path.basename(map_path)} (--reuse-map)", flush=True)
    else:
        con.execute(f"""copy (
            select owner_norm, md5(entity_name) entity_id, entity_name from (
              select owner_norm, {NAME_CORE} as entity_name
              from (select distinct owner_norm from {base} where {notsent})
            ) where entity_name <> ''
          ) to '{map_path}' (format parquet)""")

    # The entity rollup is JOIN-FREE: entity_name is computed inline (NAME_CORE) inside a single
    # streaming GROUP BY -- the same shape as owner_summary, which fits this 16.8G box. The earlier
    # approach joined 102M parcels to the 65M-row map (a ~5G hash build side) and was OOM-killed even
    # while spilling, because the box is RAM-bound and already paging. No join here -> no big build
    # side. owner_variants uses approx_count_distinct (HyperLogLog, ~tiny per-group state; an exact
    # count would need either the map join or per-group sets, both memory-heavy) -- it is an
    # informational variant count, so ~1.6% sketch error is fine.

    # Step 1 (materialized): per-entity stamped-total EXCESS (docs/VALUE_QUALITY.md) -- a large value
    # repeated on many parcels within (entity, county) should count once. De-dup is at the ENTITY
    # grain (not owner_norm) so one installation stamped under two name spellings is not double-
    # counted. Pre-filtered to large values, so this pass is tiny.
    con.execute(f"""create temp table _entity_excess as
        select entity_name, sum((c - 1) * av) excess from (
          select entity_name, county_fips, av, count(*) c from (
            select {NAME_CORE} as entity_name, county_fips,
                   try_cast(assessed_value as bigint) av
            from {base}
            where {notsent} and try_cast(assessed_value as bigint) >= {STAMP_MIN_VALUE}
          ) where entity_name <> ''
          group by entity_name, county_fips, av
          having count(*) >= {STAMP_MIN_COUNT}
        ) group by entity_name""")

    # Step 2 -- entity rollup (single streaming GROUP BY entity_name) + de-duped value = naive minus
    # the stamped excess (small LEFT JOIN to the tiny materialized table above).
    con.execute(f"""copy (
        select s.entity_name, md5(s.entity_name) entity_id, s.owner_variants, s.parcels,
               s.total_value, s.total_value - coalesce(x.excess, 0) as total_value_dedup, s.states
        from (
          select entity_name,
                 approx_count_distinct(owner_norm) owner_variants,
                 count(*) parcels,
                 sum(try_cast(assessed_value as bigint)) total_value,
                 count(distinct state) states
          from (select {NAME_CORE} as entity_name, owner_norm, state, assessed_value
                from {base} where {notsent})
          where entity_name <> ''
          group by entity_name
        ) s left join _entity_excess x using(entity_name)
      ) to '{sum_path}' (format parquet)""")

    n_own = con.execute(f"select count(*) from read_parquet('{map_path}')").fetchone()[0]
    n_ent = con.execute(f"select count(*) from read_parquet('{sum_path}')").fetchone()[0]  # 1 row/entity
    print(f"owner_entity: {n_own:,} owner_norms -> {n_ent:,} entities "
          f"({100*(1-n_ent/n_own):.1f}% consolidation)")
    print(f"  wrote {os.path.basename(map_path)} + {os.path.basename(sum_path)}")
    return con


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--reuse-map", action="store_true",
                    help="skip rebuilding owner_entity.parquet (the map); reuse the existing one and "
                         "only rebuild the summary -- safe when only assessed_value changed")
    ap.add_argument("--from-summary", action="store_true",
                    help="RAM-light: roll owner_summary up to the entity grain (chunked) instead of "
                         "scanning all parcels; exact value/parcels, `states` is a lower bound")
    ap.add_argument("--buckets", type=int, default=8, help="hash buckets for --from-summary (more = "
                    "lower peak memory)")
    ap.add_argument("--outdir", default=OUTDIR,
                    help="write the two outputs here instead of data/parcels/owners (staged build)")
    a = ap.parse_args()
    if a.from_summary:
        build_from_summary(buckets=a.buckets, outdir=a.outdir)
    else:
        build(reuse_map=a.reuse_map, outdir=a.outdir)
