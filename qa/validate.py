#!/usr/bin/env python3
"""Regression validation suite for the normalized parcel dataset.

Three tiers, run against data/parcels/normalized/*.parquet + data/parcels/manifest.json:

  A. GROUND TRUTH   every row of qa/ground_truth.csv (a verified owner fact) must still
                    match at least min_parcels parcels via owner ILIKE pattern.
  B. COVERAGE       per-state owner/apn/assessed_value coverage must not drop more than
                    2 percentage points below the frozen baseline
                    (qa/coverage_baseline.json, snapshotted from the manifest's
                    'populated' audits — the values the 12 hand-validated states had).
  C. SANITY         no assessed_value > $20B; no whitespace/empty owner_norm; pipeline
                    sentinel owners always bracketed '[...]'; county_fips valid per
                    src/county_methods.csv and present in the manifest; no geometry
                    bbox spanning > 5 degrees (the world-spanning-ring bug class);
                    every normalized file carries the REQUIRED columns (owner_norm /
                    land_use_category are written by overlay passes, NOT normalize —
                    a re-normalize that loses them silently drops the county from any
                    national query that filters on them: the 49-file / ~3.2M-parcel
                    schema-drift incident, 2026-07-01); per-county duplicate-APN rate
                    must not grow > 2pts past qa/dup_baseline.json (duplicate-ingest
                    class: 5 regional layers / ~3M phantom parcels, fixed 2026-06-29).

Usage:
  python qa/validate.py                  # full run, all states
  python qa/validate.py --state TX       # one state only
  python qa/validate.py --skip-geometry  # skip the bbox tier (it is cheap, but optional)
  python qa/validate.py --write-baseline # snapshot manifest 'populated' -> coverage_baseline.json
                                         # (ONLY after a refresh has been hand-verified good)
  python qa/validate.py --write-dup-baseline
                                         # scan normalized data, snapshot per-county duplicate-APN
                                         # rates -> qa/dup_baseline.json (same hand-verified rule;
                                         # scans every state: minutes, not seconds)

Exit code: 0 = all checks pass, 1 = at least one FAIL (report on stdout), 2 = setup error.

Resource notes: runs DuckDB with memory_limit=2GB / threads=2 by default (override with
--memory-limit / --threads) so it can run alongside heavy jobs. Full 12-state run scans
~48M rows of 5 columns; expect minutes, not seconds.
"""
import argparse
import csv
import glob as globmod
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NORM_DIR = os.path.join(ROOT, "data", "parcels", "normalized")
MANIFEST = os.path.join(ROOT, "data", "parcels", "manifest.json")
GROUND_TRUTH = os.path.join(ROOT, "qa", "ground_truth.csv")
BASELINE = os.path.join(ROOT, "qa", "coverage_baseline.json")
DUP_BASELINE = os.path.join(ROOT, "qa", "dup_baseline.json")
KNOWN_ISSUES = os.path.join(ROOT, "qa", "known_issues.csv")
COUNTY_METHODS = os.path.join(ROOT, "src", "county_methods.csv")

COVERAGE_TOLERANCE_PTS = 2.0      # tier B: state-level regression tolerance
COUNTY_WARN_PTS = 5.0             # tier B: per-county drop that earns a warning line
DUP_TOLERANCE_PTS = 2.0           # tier C: per-county duplicate-APN growth past baseline
DUP_MIN_EXCESS_ROWS = 1000        # tier C: ignore dup growth below this (tiny-county noise)
DUP_NEW_COUNTY_WARN_PCT = 10.0    # tier C: county with no dup baseline gets a warn above this

# Columns every normalized parquet MUST carry. owner_norm/land_use_category are written by
# overlay passes (owners_std / landuse_std), not normalize itself; their absence means a
# re-normalize ran without the follow-up passes and the county silently vanishes from any
# national query touching those columns.
REQUIRED_COLS = ("state", "county_fips", "apn", "owner", "owner_norm",
                 "assessed_value", "land_use_category", "geometry")
VALUE_CAP = 20_000_000_000        # tier C: no single parcel worth > $20B (raised from $5B for NYC
                                  # mega-parcels — JFK Airport/Port Authority assesses at $8.5B,
                                  # LaGuardia $4.3B; cents-as-dollars errors land ~100x higher)
BBOX_MAX_DEG = 5.0                # tier C: legit aggregate parcels span <= ~1.8 deg

# Phrases the PIPELINE itself writes as owner placeholders. The sentinel convention
# (CONTINUE.md, 2026-06-10) is that every pipeline placeholder is bracketed '[...]' so it
# can never be mistaken for a real name and is excludable with owner NOT LIKE '[%'.
# An unbracketed occurrence means a normalize/ingest path skipped the rewrite.
UNBRACKETED_SENTINELS = (
    "INDIVIDUAL OWNER",        # Jefferson WA raw placeholder (OWNER_SENTINEL_REWRITES)
    "NOT PUBLISHED",
    "SUPPRESSED",
    "OWNER SUPPRESSED",
    "OWNER NOT PUBLISHED",
)

# Plausible lon/lat window per state, margins included (~0.5 deg). A geometry whose bbox
# falls outside its state's window is DISPLACED (bad reprojection/junk vertex) even when
# its own span is small — the El Paso-parcels-in-the-Pacific class, which the 5-degree
# span rule alone cannot see. States not listed get the span-only check plus a warning.
STATE_WINDOW = {  # (west, south, east, north)
    "WA": (-125.5, 45.0, -116.0, 49.5), "OR": (-125.4, 41.4, -115.8, 46.9),
    "MT": (-116.9, 43.7, -103.2, 49.5), "ID": (-118.1, 41.4, -110.2, 49.5),
    "WY": (-111.9, 40.4, -103.2, 45.8), "WV": (-83.4, 36.7, -77.1, 41.4),
    "WI": (-93.6, 42.0, -86.1, 47.8),   "TN": (-91.1, 34.4, -80.9, 37.4),
    "AR": (-95.4, 32.4, -88.9, 37.3),   "NC": (-85.1, 33.2, -74.8, 37.4),
    "FL": (-88.4, 24.0, -79.3, 31.8),   "TX": (-107.5, 25.3, -92.8, 37.2),
    "AZ": (-114.9, 31.2, -109.0, 37.05), "CO": (-109.1, 36.9, -102.0, 41.05),
    "NM": (-109.1, 31.2, -102.9, 37.05), "NV": (-120.1, 34.9, -114.0, 42.05),
    "UT": (-114.1, 36.9, -109.0, 42.05),
    "MO": (-95.85, 35.95, -89.05, 40.65), "IN": (-88.15, 37.75, -84.75, 41.80),
    "SC": (-83.40, 31.95, -78.45, 35.25), "PA": (-80.55, 39.65, -74.65, 42.30),
    "GA": (-85.65, 30.30, -80.80, 35.05), "MI": (-90.45, 41.65, -82.10, 48.35),
    "AK": (-167.5, 53.0, -129.5, 71.6),  # data bounds verified 2026-06-20 (no antimeridian wrap)
    "ND": (-104.10, 45.90, -96.50, 49.05), "MS": (-91.70, 30.10, -88.05, 35.05),
    "NY": (-79.80, 40.45, -71.80, 45.05),
    "DC": (-77.13, 38.78, -76.90, 39.01), "VT": (-73.45, 42.72, -71.45, 45.03),
    "IA": (-96.65, 40.36, -90.13, 43.52), "MN": (-97.25, 43.48, -89.48, 49.40),
    "MA": (-73.52, 41.17, -69.85, 42.90), "IL": (-91.53, 36.96, -87.48, 42.52),
    "OK": (-103.01, 33.61, -94.42, 37.01),
    # added 2026-07-01 (state bounds + ~0.5 deg margin, same convention as above)
    "AL": (-88.98, 30.14, -84.39, 35.51), "CA": (-124.91, 32.03, -113.63, 42.51),
    "CT": (-74.23, 40.49, -71.29, 42.55), "DE": (-76.06, 38.05, -74.55, 40.34),
    "KS": (-102.55, 36.49, -94.09, 40.50), "KY": (-89.90, 36.00, -81.46, 39.65),
    "LA": (-94.54, 28.43, -88.32, 33.52), "MD": (-79.99, 37.39, -74.55, 40.22),
    "NE": (-104.55, 39.50, -95.01, 43.50), "NH": (-72.90, 42.20, -70.10, 45.81),
    "NJ": (-75.86, 38.43, -73.39, 41.86), "OH": (-85.32, 37.90, -80.02, 42.48),
    "RI": (-72.39, 40.65, -70.62, 42.52), "SD": (-104.56, 42.03, -95.80, 46.45),
    "VA": (-83.98, 35.99, -74.74, 39.97),
    # added 2026-07-03 (new jurisdictions from the backlog campaign)
    "ME": (-71.58, 42.47, -66.45, 47.96), "HI": (-160.75, 18.40, -154.30, 22.74),
}


def sql_quote(s):
    return "'" + str(s).replace("'", "''") + "'"


def state_prefix_map(manifest):
    """state abbrev -> 2-digit FIPS prefix, from the manifest itself."""
    m = {}
    for fips, e in manifest.items():
        st = e.get("state")
        if st:
            m.setdefault(st, fips[:2])
    return m


def files_for_prefix(prefix):
    return sorted(globmod.glob(os.path.join(NORM_DIR, prefix + "*.parquet")))


class Report:
    def __init__(self):
        self.fails, self.warns, self.infos = [], [], []

    def fail(self, tier, msg):
        self.fails.append((tier, msg))
        print(f"  FAIL [{tier}] {msg}")

    def warn(self, tier, msg):
        self.warns.append((tier, msg))
        print(f"  warn [{tier}] {msg}")

    def info(self, msg):
        self.infos.append(msg)
        print(f"  {msg}")


RAW_INVENTORY = os.path.join(ROOT, "data", "parcels", "raw_inventory.json")


def load_raw_inventory():
    """{raw basename: {rows, bytes, source, ...}} from src/raw_inventory.py; empty if absent."""
    if os.path.exists(RAW_INVENTORY):
        with open(RAW_INVENTORY, encoding="utf-8") as f:
            return json.load(f)
    return {}


def load_known_issues():
    """Pre-existing, documented data issues (see FINDINGS.md): (check, county_fips) ->
    (allowed_count, note). A matching failure at or below allowed_count downgrades to a
    warning; anything new or grown still FAILS."""
    out = {}
    if os.path.exists(KNOWN_ISSUES):
        with open(KNOWN_ISSUES, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                out[(r["check"], r["county_fips"])] = (int(r["allowed_count"]), r["note"])
    return out


def known(rep, issues, check, fips, n, msg):
    """Route a tier-C finding: known+within-allowance -> warn, else FAIL."""
    allowed, note = issues.get((check, fips), (0, ""))
    if 0 < n <= allowed:
        rep.warn("C", f"{msg} — KNOWN ISSUE (allowed {allowed}): {note}")
    elif n:
        rep.fail("C", msg + (f" — grew past known allowance {allowed} ({note})"
                             if allowed else ""))


def load_ground_truth():
    rows = []
    with open(GROUND_TRUTH, newline="", encoding="utf-8") as f:
        for i, r in enumerate(csv.DictReader(f)):
            r["_id"] = f"gt{i:03d}"
            r["min_parcels"] = int(r["min_parcels"])
            r["county_fips"] = (r.get("county_fips") or "").strip()
            rows.append(r)
    return rows


def write_baseline(manifest):
    """Snapshot the manifest's per-county 'populated' audits as the frozen baseline.

    The manifest is REWRITTEN by every normalize run, so comparing fresh data against the
    fresh manifest would be self-confirming. This file freezes the known-good numbers; only
    regenerate it after a refresh has been hand-verified.
    """
    counties, states = {}, defaultdict(lambda: {"n": 0.0, "owner": 0.0, "apn": 0.0,
                                                "assessed_value": 0.0, "counties": 0})
    missing = []
    for fips, e in sorted(manifest.items()):
        pop, st = e.get("populated"), e.get("state")
        n = e.get("feature_count") or 0
        if not pop or not st or not n:
            missing.append(fips)
            continue
        counties[fips] = {"state": st, "n": n,
                          "owner": pop.get("owner", 0.0), "apn": pop.get("apn", 0.0),
                          "assessed_value": pop.get("assessed_value", 0.0)}
        s = states[st]
        s["counties"] += 1
        s["n"] += n
        for k in ("owner", "apn", "assessed_value"):
            s[k] += counties[fips][k] * n
    out_states = {}
    for st, s in states.items():
        out_states[st] = {"counties": s["counties"], "parcels": int(s["n"]),
                          **{k: round(100.0 * s[k] / s["n"], 2)
                             for k in ("owner", "apn", "assessed_value")}}
    payload = {"snapshot_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
               "source": "data/parcels/manifest.json field 'populated' (fraction non-null, "
                         "weighted by feature_count)",
               "states": out_states, "counties": counties}
    with open(BASELINE, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=1, sort_keys=True)
    print(f"Baseline written: {BASELINE}  ({len(counties)} counties, "
          f"{len(out_states)} states{'; SKIPPED no-audit fips: ' + ','.join(missing) if missing else ''})")


def tier_a(con, gt_rows, states, rep):
    print("\n=== TIER A: GROUND TRUTH ===")
    by_state = defaultdict(list)
    for r in gt_rows:
        by_state[r["state"]].append(r)
    for st in sorted(by_state):
        if st not in states:
            for r in by_state[st]:
                rep.fail("A", f"{st}: state not in dataset, ground-truth row "
                              f"'{r['owner_pattern']}' unverifiable")
            continue
        files = states[st]["files"]
        rows = by_state[st]
        vals = ", ".join(f"({sql_quote(r['_id'])}, {sql_quote(r['owner_pattern'])}, "
                         f"{sql_quote(r['county_fips'])})" for r in rows)
        flist = ", ".join(sql_quote(f) for f in files)
        q = f"""
        SELECT t.id, count(*) n
        FROM read_parquet([{flist}], union_by_name=true) p
        JOIN (VALUES {vals}) t(id, pat, cf)
          ON p.owner ILIKE t.pat AND (t.cf = '' OR p.county_fips = t.cf)
        GROUP BY 1"""
        counts = dict(con.execute(q).fetchall())
        for r in rows:
            n, where = counts.get(r["_id"], 0), (r["county_fips"] or "statewide")
            label = f"{st} {where} '{r['owner_pattern']}'"
            if n >= r["min_parcels"]:
                print(f"  ok   {label}: {n} >= {r['min_parcels']}  ({r['note']})")
            else:
                rep.fail("A", f"{label}: {n} < min {r['min_parcels']} — {r['note']} "
                              f"[source: {r['source']}]")


def scan_state(con, files):
    """One pass over a state's parquet computing everything tiers B and C1-C4 need."""
    flist = ", ".join(sql_quote(f) for f in files)
    sent = ", ".join(sql_quote(s) for s in UNBRACKETED_SENTINELS)
    q = f"""
    SELECT county_fips,
           count(*)                AS rows,
           count(owner)            AS owner_nn,
           count(apn)              AS apn_nn,
           count(DISTINCT apn)     AS apn_distinct,
           count(assessed_value)   AS value_nn,
           max(TRY_CAST(assessed_value AS DOUBLE))                                AS max_value,
           sum(CASE WHEN TRY_CAST(assessed_value AS DOUBLE) > {VALUE_CAP}
                    THEN 1 ELSE 0 END)                                            AS over_cap,
           sum(CASE WHEN owner_norm IS NOT NULL AND trim(owner_norm) = ''
                    THEN 1 ELSE 0 END)                                            AS blank_norm,
           sum(CASE WHEN owner LIKE '[%' AND owner NOT LIKE '%]%'
                    THEN 1 ELSE 0 END)                                            AS bracket_bad,
           sum(CASE WHEN upper(trim(owner)) IN ({sent}) THEN 1 ELSE 0 END)        AS unbracketed,
           sum(CASE WHEN county_fips IS NULL THEN 1 ELSE 0 END)                   AS null_fips
    FROM read_parquet([{flist}], union_by_name=true)
    GROUP BY 1"""
    out = {}
    for row in con.execute(q).fetchall():
        out[row[0]] = dict(zip(("rows", "owner_nn", "apn_nn", "apn_distinct", "value_nn",
                                "max_value", "over_cap", "blank_norm", "bracket_bad",
                                "unbracketed", "null_fips"), row[1:]))
    return out


def dup_pct(c):
    """Duplicate-APN rate for one county's scan stats: % of non-null-apn rows that are
    excess copies (rows beyond the first per APN). Condo/common-area counties legitimately
    sit above zero, which is why the check is growth-vs-baseline, not an absolute cap."""
    return 100.0 * (c["apn_nn"] - c["apn_distinct"]) / c["apn_nn"] if c["apn_nn"] else 0.0


def write_dup_baseline(con, states):
    """Snapshot per-county duplicate-APN rates from the NORMALIZED DATA (not the manifest —
    the manifest has no dup dimension). Frozen for the same reason as coverage_baseline:
    a fresh scan compared against a fresh scan is self-confirming."""
    counties = {}
    for st in sorted(states):
        stats = scan_state(con, states[st]["files"])
        for fips, c in stats.items():
            counties[fips] = {"state": st, "apn_nn": int(c["apn_nn"]),
                              "dup_pct": round(dup_pct(c), 3)}
        print(f"  scanned {st}: {len(stats)} counties")
    payload = {"snapshot_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
               "source": "data/parcels/normalized (count(apn) - count(DISTINCT apn) per county)",
               "counties": counties}
    with open(DUP_BASELINE, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=1, sort_keys=True)
    print(f"Dup baseline written: {DUP_BASELINE}  ({len(counties)} counties)")


def tier_b(stats_by_state, baseline, rep):
    print("\n=== TIER B: COVERAGE FLOORS (vs frozen baseline) ===")
    for st in sorted(stats_by_state):
        base = baseline["states"].get(st)
        if not base:
            rep.warn("B", f"{st}: no baseline recorded — run --write-baseline after verifying")
            continue
        stats = stats_by_state[st]
        rows = sum(c["rows"] for c in stats.values())
        if not rows:
            rep.fail("B", f"{st}: zero rows on disk")
            continue
        cur = {k: 100.0 * sum(c[f"{k0}_nn"] for c in stats.values()) / rows
               for k, k0 in (("owner", "owner"), ("apn", "apn"), ("assessed_value", "value"))}
        msgs = []
        for k in ("owner", "apn", "assessed_value"):
            drop = base[k] - cur[k]
            tag = "ok  " if drop <= COVERAGE_TOLERANCE_PTS else "FAIL"
            msgs.append(f"{k} {cur[k]:.1f}% (base {base[k]:.1f}%)")
            if drop > COVERAGE_TOLERANCE_PTS:
                rep.fail("B", f"{st} {k} coverage {cur[k]:.2f}% dropped "
                              f"{drop:.2f}pts below baseline {base[k]:.2f}%")
                worst = sorted(((f, baseline["counties"].get(f, {}).get(k, 0) * 100
                                 - 100.0 * c[{"owner": "owner_nn", "apn": "apn_nn",
                                              "assessed_value": "value_nn"}[k]] / c["rows"])
                                for f, c in stats.items() if c["rows"]),
                               key=lambda x: -x[1])[:5]
                rep.info(f"    worst counties for {st} {k}: "
                         + ", ".join(f"{f} (-{d:.1f}pts)" for f, d in worst if d > 0))
        # county completeness: every baseline county must still exist with rows
        missing = [f for f, b in baseline["counties"].items()
                   if b["state"] == st and (f not in stats or stats[f]["rows"] == 0)]
        if missing:
            rep.fail("B", f"{st}: counties in baseline but missing/empty now: "
                          f"{', '.join(sorted(missing))}")
        new = [f for f in stats if f and f not in baseline["counties"]]
        extra = f"; new counties not in baseline: {', '.join(sorted(new))}" if new else ""
        print(f"  {st}: {rows:,} rows | " + " | ".join(msgs)
              + (f" | {len(missing)} MISSING county(ies)" if missing else "") + extra)


def tier_c_schema(states, rep):
    """Every normalized file must carry REQUIRED_COLS (parquet metadata only — no data scan)."""
    import pyarrow.parquet as pq
    bad = 0
    for st in sorted(states):
        for path in states[st]["files"]:
            cols = set(pq.ParquetFile(path).schema_arrow.names)
            miss = [c for c in REQUIRED_COLS if c not in cols]
            if miss:
                bad += 1
                rep.fail("C", f"{os.path.basename(path)}: missing required column(s) "
                              f"{', '.join(miss)} — re-run the overlay passes "
                              f"(owners_std / landuse_std) after any re-normalize")
    if not bad:
        print(f"  ok   required columns present in every file "
              f"({sum(len(s['files']) for s in states.values())} files checked)")


def tier_c_shortfall(states, manifest, rep, issues):
    """Disk rows vs manifest feature_count (parquet footer only — no data scan). A file far
    short of what the source reported is the truncated-pull class (paging cap / dropped chunk),
    which nothing else in the suite can see: coverage %s stay flat when rows vanish
    proportionally. Intentional raw dedups (double-ingest vintages, 2026-07-03) are recorded in
    known_issues.csv as check=row_shortfall with the exact allowed shortfall."""
    import pyarrow.parquet as pq
    by_norm = {os.path.basename(e.get("normalized_file") or ""): (fips, e)
               for fips, e in manifest.items()}
    inventory = load_raw_inventory()
    checked = flagged = 0
    evidence = {"local raw": 0, "archived raw (inventory)": 0, "normalized rows only": 0}
    for st in sorted(states):
        for path in states[st]["files"]:
            fips, e = by_norm.get(os.path.basename(path), (None, None))
            ref = (e or {}).get("expected_count")
            if ref is None:
                ref = (e or {}).get("reported_count")
            if ref is None and (e or {}).get("count_status") == "verified":
                ref = (e or {}).get("fetched_count")
            if ref is None:
                rep.warn("C", f"{st} {fips}: no source expected/reported count; release unverified")
                continue
            raw_rel = (e or {}).get("file")
            raw_path = os.path.join(ROOT, raw_rel.replace("/", os.sep)) if raw_rel else None
            raw_name = os.path.basename(raw_rel) if raw_rel else None
            checked += 1
            # Evidence order (2026-09-14): local raw footer -> archived raw footer recorded in
            # data/parcels/raw_inventory.json (R2) -> normalized footer. Raw is deliberately
            # deleted after a verified R2 push, so "missing locally" is not itself a defect;
            # an unexplained SHORTFALL against the source count is, whatever the evidence tier.
            if raw_path and os.path.exists(raw_path):
                rows, how = pq.read_metadata(raw_path).num_rows, "local raw"
            elif raw_name in inventory and inventory[raw_name].get("rows") is not None:
                rows, how = int(inventory[raw_name]["rows"]), "archived raw (inventory)"
            else:
                rows, how = pq.read_metadata(path).num_rows, "normalized rows only"
            evidence[how] += 1
            # Normalize must not silently shed rows either: raw-evidence rows minus normalized rows
            # must be explained by a declared parcel-grain collapse (manifest grain_dropped_count,
            # normalize.GRAIN_KEYS) or a known row_shortfall exception. Warn-only (2026-09-15).
            if how != "normalized rows only":
                norm_rows = pq.read_metadata(path).num_rows
                grain = int((e or {}).get("grain_dropped_count") or 0)
                unexplained = rows - norm_rows - grain
                if unexplained > max(25, 0.005 * rows) and ("row_shortfall", fips) not in issues:
                    rep.warn("C", f"{st} {fips}: normalized {norm_rows:,} vs {how} {rows:,} "
                                  f"(grain collapse {grain:,}): {unexplained:,} rows dropped in "
                                  f"normalize without a declared grain key or known issue")
            short = ref - rows
            if short != 0:
                flagged += 1
                known(rep, issues, "row_shortfall", fips, short,
                      f"{st} {fips}: {how} {rows:,} vs expected source count {int(ref):,} "
                      f"(delta {-short:+,}); re-pull (refresh.py --pull --force-fips) or document "
                      f"the source exception in known_issues.csv")
    tiers = ", ".join(f"{v} via {k}" for k, v in evidence.items() if v)
    if not flagged:
        print(f"  ok   rows exactly reconcile to source counts ({checked} files: {tiers})")
    else:
        rep.info(f"row reconciliation evidence: {tiers}; {flagged} shortfall(s) above")


def tier_c_owner_wipe(states, rep):
    """owner_norm must be POPULATED wherever owner is (footer null-count stats only). The
    column-presence check above can't see the all-NULL variant (normalize types all-null
    columns INT32), which silently drops the county from owner_norm-filtering queries."""
    import pyarrow.parquet as pq
    checked = flagged = nostats = 0
    for st in sorted(states):
        for path in states[st]["files"]:
            md = pq.read_metadata(path)
            names = {md.schema.column(i).name: i for i in range(md.num_columns)}
            if "owner" not in names or "owner_norm" not in names:
                continue  # absence is tier_c_schema's finding
            nn = {}
            ok = True
            for col in ("owner", "owner_norm"):
                nulls = 0
                for rg in range(md.num_row_groups):
                    s = md.row_group(rg).column(names[col]).statistics
                    if s is None or s.null_count is None:
                        ok = False
                        break
                    nulls += s.null_count
                if not ok:
                    break
                nn[col] = md.num_rows - nulls
            if not ok:
                nostats += 1
                continue
            checked += 1
            if nn["owner"] > 1000 and nn["owner_norm"] == 0:
                flagged += 1
                rep.fail("C", f"{st} {os.path.basename(path)}: owner populated "
                              f"({nn['owner']:,}) but owner_norm 100% NULL — owners_std "
                              f"was not re-run after a re-normalize (wipe class)")
    if not flagged:
        print(f"  ok   owner_norm populated wherever owner is ({checked} files"
              + (f", {nostats} without footer stats skipped" if nostats else "") + ")")


def tier_c_dups(stats_by_state, dup_baseline, rep):
    """Per-county duplicate-APN growth vs frozen dup_baseline.json. Catches duplicate-row
    regressions Tier D's static slicer check cannot see (double-append ingests, fan-out joins
    in backfills/overlays)."""
    if dup_baseline is None:
        rep.warn("C", "qa/dup_baseline.json missing — duplicate-APN regression check skipped; "
                      "run --write-dup-baseline on a verified dataset")
        return
    base = dup_baseline["counties"]
    checked = flagged = 0
    for st in sorted(stats_by_state):
        for fips, c in stats_by_state[st].items():
            if not fips or not c["apn_nn"]:
                continue
            checked += 1
            cur = dup_pct(c)
            b = base.get(fips)
            if b is None:
                if cur > DUP_NEW_COUNTY_WARN_PCT:
                    rep.warn("C", f"{st} {fips}: new county (no dup baseline) with "
                                  f"{cur:.1f}% duplicate-APN rows — eyeball before "
                                  f"--write-dup-baseline")
                continue
            growth = cur - b["dup_pct"]
            excess_rows = c["apn_nn"] - c["apn_distinct"]
            base_excess = b["apn_nn"] * b["dup_pct"] / 100.0
            if growth > DUP_TOLERANCE_PTS and excess_rows - base_excess > DUP_MIN_EXCESS_ROWS:
                flagged += 1
                rep.fail("C", f"{st} {fips}: duplicate-APN rate {cur:.1f}% grew "
                              f"{growth:.1f}pts past baseline {b['dup_pct']:.1f}% "
                              f"({excess_rows:,} excess rows — duplicate-ingest class?)")
    if not flagged:
        print(f"  ok   duplicate-APN rates within baseline ({checked} counties)")


def tier_c(con, stats_by_state, states, manifest, rep, skip_geometry, issues):
    print("\n=== TIER C: SANITY ===")
    tier_c_schema(states, rep)
    tier_c_shortfall(states, manifest, rep, issues)
    tier_c_owner_wipe(states, rep)
    valid_fips = set()
    with open(COUNTY_METHODS, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            valid_fips.add(r["fips"])

    for st in sorted(stats_by_state):
        for fips, c in sorted(stats_by_state[st].items(), key=lambda kv: kv[0] or ""):
            where = f"{st} {fips}"
            known(rep, issues, "value_cap", fips, c["over_cap"] or 0,
                  f"{where}: {c['over_cap']} parcel(s) assessed_value > ${VALUE_CAP:,} "
                  f"(max ${(c['max_value'] or 0):,.0f})")
            if c["blank_norm"]:
                rep.fail("C", f"{where}: {c['blank_norm']} owner_norm value(s) are "
                              f"whitespace/empty (should be NULL or real)")
            known(rep, issues, "bracket", fips, c["bracket_bad"] or 0,
                  f"{where}: {c['bracket_bad']} owner(s) start '[' but never close ']' "
                  f"(malformed sentinel)")
            known(rep, issues, "sentinel", fips, c["unbracketed"] or 0,
                  f"{where}: {c['unbracketed']} owner(s) equal a pipeline sentinel "
                  f"phrase WITHOUT brackets (rewrite skipped?)")
            if c["null_fips"]:
                rep.fail("C", f"{where}: {c['null_fips']} row(s) with NULL county_fips")
            if fips and fips not in valid_fips:
                rep.fail("C", f"{where}: county_fips not present in src/county_methods.csv")
            if fips and fips not in manifest:
                rep.fail("C", f"{where}: county_fips not present in manifest.json")
    print(f"  value cap / owner_norm blanks / sentinel bracketing / county_fips: checked "
          f"{sum(len(s) for s in stats_by_state.values())} counties")

    if skip_geometry:
        print("  geometry bbox: SKIPPED (--skip-geometry)")
        return
    print("  geometry screen (file-level metadata bbox; span>5deg or outside the state "
          "window => per-row drill):")
    import pyarrow.parquet as pq
    spatial_ok = None
    flagged = 0
    for st in sorted(stats_by_state):
        win = STATE_WINDOW.get(st)
        if not win:
            rep.warn("C", f"{st}: no STATE_WINDOW entry — displaced-geometry check "
                          f"reduced to span-only; add the state's bbox to validate.py")
        for path in states[st]["files"]:
            md = pq.read_metadata(path).metadata
            bbox = None
            if md and b"geo" in md:
                bbox = json.loads(md[b"geo"])["columns"]["geometry"].get("bbox")
            if bbox is not None:
                span_ok = (bbox[2] - bbox[0] <= BBOX_MAX_DEG
                           and bbox[3] - bbox[1] <= BBOX_MAX_DEG)
                win_ok = (win is None or (bbox[0] >= win[0] and bbox[1] >= win[1]
                                          and bbox[2] <= win[2] and bbox[3] <= win[3]))
                if span_ok and win_ok:
                    continue   # file bbox bounds every row => all rows pass both checks
            else:
                rep.warn("C", f"{os.path.basename(path)}: no geo bbox metadata — "
                              f"falling back to per-row scan")
            flagged += 1
            if spatial_ok is None:
                try:
                    con.execute("INSTALL spatial; LOAD spatial")
                    spatial_ok = True
                except Exception as e:
                    spatial_ok = False
                    rep.warn("C", f"duckdb spatial unavailable ({e}); bbox drill skipped")
            if not spatial_ok:
                rep.fail("C", f"{os.path.basename(path)}: file bbox suspect ({bbox}) "
                              f"and per-row drill unavailable")
                continue
            wincond = ("FALSE" if win is None else
                       f"ST_XMin(g) < {win[0]} OR ST_YMin(g) < {win[1]} "
                       f"OR ST_XMax(g) > {win[2]} OR ST_YMax(g) > {win[3]}")
            # spatial reads geoparquet 'geometry' as GEOMETRY; fall back to WKB blobs
            for geom_expr in ("geometry", "ST_GeomFromWKB(geometry)"):
                try:
                    n, sample = con.execute(f"""
                        SELECT count(*), min(apn) FROM (
                          SELECT apn, {geom_expr} g
                          FROM read_parquet({sql_quote(path)}) WHERE geometry IS NOT NULL)
                        WHERE ST_XMax(g) - ST_XMin(g) > {BBOX_MAX_DEG}
                           OR ST_YMax(g) - ST_YMin(g) > {BBOX_MAX_DEG}
                           OR {wincond}""").fetchone()
                    break
                except Exception:
                    n = None
            fips = os.path.basename(path)[:5]
            if n is None:
                rep.fail("C", f"{os.path.basename(path)}: per-row geometry drill errored")
            elif n:
                known(rep, issues, "bbox", fips, n,
                      f"{os.path.basename(path)}: {n} geometry(ies) span >5deg or sit "
                      f"outside the {st} window (displaced/world-spanning junk; "
                      f"e.g. apn {sample})")
            else:
                rep.info(f"    {os.path.basename(path)}: file bbox suspect but no "
                         f"single row trips span/window — ok")
    print(f"    {flagged} file(s) needed a per-row drill; all others pass by metadata")


def tier_d_crosswalk_slicers(rep):
    """Tier D (STATIC — no data scan): catch the duplicate-ingest bug CLASS at its source. A
    statewide/regional ArcGIS layer mapped to >=2 counties MUST have a per-county slicer
    (ingest._statewide_where); without one, every county pulls the WHOLE layer and ends up with
    identical, mislabeled data (VA HRGeo / KS x911 / NY DANC / SD districtiii, all fixed 2026-06-29).
    Known separate-path layers (TX -> ingest_bulk_tx, PR -> not on the standard path) are allowlisted."""
    print("\n(tier D: crosswalk shared-layer slicers — static check)...")
    ALLOW = ("feature.geographic.texas.gov", "paralanaturaleza.org")
    try:
        sys.path.insert(0, os.path.join(ROOT, "src"))
        import ingest
    except Exception as e:  # never let an import dep break the whole suite
        rep.warn("D", f"could not import src/ingest.py to verify slicers: {e}")
        return
    by_url = defaultdict(list)
    with open(COUNTY_METHODS, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if (r.get("method") or "") == "arcgis" and (r.get("query_url") or "").strip():
                by_url[r["query_url"].strip()].append(r)
    bad = 0
    for url, rs in by_url.items():
        if len(rs) < 2 or any(a in url for a in ALLOW):
            continue
        if ingest._statewide_where(rs[0]) is None:
            bad += 1
            counties = ", ".join(f"{r['county']}/{r['state']}" for r in rs[:8])
            rep.fail("D", f"{len(rs)} counties share an ArcGIS layer with NO slicer (each pulls the "
                          f"whole layer -> duplicate-ingest bug): {counties}  [{url[:55]}]")
    if not bad:
        print("  ok  [D] every shared ArcGIS layer (>=2 counties) has a per-county slicer")


def main():
    # Windows consoles default to cp1252; force UTF-8 so report text survives
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--state", help="two-letter state code, limit the run")
    ap.add_argument("--skip-geometry", action="store_true")
    ap.add_argument("--write-baseline", action="store_true",
                    help="snapshot manifest 'populated' audits to qa/coverage_baseline.json")
    ap.add_argument("--write-dup-baseline", action="store_true",
                    help="scan normalized data, snapshot per-county duplicate-APN rates "
                         "to qa/dup_baseline.json")
    ap.add_argument("--memory-limit", default="2GB")
    ap.add_argument("--threads", type=int, default=2)
    args = ap.parse_args()

    for p in (MANIFEST, COUNTY_METHODS):
        if not os.path.exists(p):
            print(f"setup error: missing {p}")
            return 2
    with open(MANIFEST, encoding="utf-8") as f:
        manifest = json.load(f)

    if args.write_baseline:
        write_baseline(manifest)
        return 0

    if not args.write_dup_baseline:
        if not os.path.exists(BASELINE):
            print(f"setup error: {BASELINE} missing — run with --write-baseline first "
                  f"(only when the current dataset is known-good)")
            return 2
        with open(BASELINE, encoding="utf-8") as f:
            baseline = json.load(f)
        if not os.path.exists(GROUND_TRUTH):
            print(f"setup error: {GROUND_TRUTH} missing")
            return 2

    import duckdb
    con = duckdb.connect()
    con.execute(f"SET memory_limit={sql_quote(args.memory_limit)}")
    con.execute(f"SET threads={args.threads}")

    prefixes = state_prefix_map(manifest)
    wanted = [args.state.upper()] if args.state else sorted(prefixes)
    states = {}
    for st in wanted:
        if st not in prefixes:
            print(f"setup error: state {st} not in manifest")
            return 2
        files = files_for_prefix(prefixes[st])
        if files:
            states[st] = {"files": files}

    if args.write_dup_baseline:
        write_dup_baseline(con, states)
        return 0

    rep = Report()
    gt = [r for r in load_ground_truth() if not args.state or r["state"] == args.state.upper()]
    tier_a(con, gt, states, rep)

    print("\n(scanning states for coverage + sanity — one pass per state...)")
    stats_by_state = {}
    for st in sorted(states):
        stats_by_state[st] = scan_state(con, states[st]["files"])
        print(f"  scanned {st}: {sum(c['rows'] for c in stats_by_state[st].values()):,} rows "
              f"across {len(stats_by_state[st])} counties")

    tier_b(stats_by_state, baseline, rep)
    tier_c(con, stats_by_state, states, manifest, rep, args.skip_geometry,
           load_known_issues())
    dup_baseline = None
    if os.path.exists(DUP_BASELINE):
        with open(DUP_BASELINE, encoding="utf-8") as f:
            dup_baseline = json.load(f)
    tier_c_dups(stats_by_state, dup_baseline, rep)
    tier_d_crosswalk_slicers(rep)

    print("\n=== SUMMARY ===")
    print(f"ground-truth rows checked: {len(gt)}")
    print(f"FAILURES: {len(rep.fails)}   warnings: {len(rep.warns)}")
    for tier, msg in rep.fails:
        print(f"  FAIL [{tier}] {msg}")
    if not rep.fails:
        print("ALL CHECKS PASSED")
    return 1 if rep.fails else 0


if __name__ == "__main__":
    sys.exit(main())
