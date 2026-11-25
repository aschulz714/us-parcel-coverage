#!/usr/bin/env python3
"""Tier-A2 owner-family floor validator (PROPOSED, standalone).

Closes the QA gap that Tier A leaves open: Tier A floors test the RAW `owner` field, so
nothing currently guards the `owner_family` assignments. This checks that the big, stable
institutional families still aggregate to at least a floor of parcels (and span at least a
floor of states), the same regression idea as the ground-truth tier — calibrated to
conservative fractions of today's measured values.

Reads ONLY: data/parcels/owners/owner_summary.parquet  and  qa/family_floors_proposed.csv
(no normalized parquets, no manifest needed, no pipeline). DuckDB pinned 2GB / 2 threads.
The lead reviews this and folds it into qa/validate.py after the 282-rule summary rebuild;
that same commit flips the SFR rows from PENDING-REBUILD to a date (activates them).

Matching: each floor's `family` is matched as `owner_family = X OR owner_norm = X`, so it
works both for familied entities (WEYERHAEUSER -> 144 spellings) and for canonical names
that have no family label yet (UNITED STATES OF AMERICA is owner_norm-only pre-rebuild).

State proxy: the summary stores `states` per owner_norm, NOT per family. `max(states)`
across the match is therefore a LOWER BOUND on the family's true state span; floors in the
CSV are set <= today's measured max(states) so this never false-fails.

Output conventions mirror qa/validate.py: per-row ok/FAIL/skip lines, a SUMMARY block, and
exit 0 (all pass), 1 (>=1 fail), 2 (setup error).

Usage:  python qa/validate_family_tier_proposed.py
        python qa/validate_family_tier_proposed.py --floors qa/family_floors_proposed.csv
"""
import argparse
import csv
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SUMMARY = os.path.join(ROOT, "data", "parcels", "owners", "owner_summary.parquet")
FLOORS = os.path.join(ROOT, "qa", "family_floors_proposed.csv")

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")   # measured_on that is a date => ACTIVE


def load_floors(path):
    """Read the floors CSV, dropping #-comment and blank lines (notes are single-line, so
    line-level filtering is safe). First surviving line is the header."""
    with open(path, newline="", encoding="utf-8") as f:
        lines = [ln for ln in f if ln.strip() and not ln.lstrip().startswith("#")]
    rows = []
    for r in csv.DictReader(lines):
        r["min_parcels"] = int(r["min_parcels"])
        r["min_states"] = int(r["min_states"])
        r["active"] = bool(DATE_RE.match((r.get("measured_on") or "").strip()))
        rows.append(r)
    return rows


def sql_quote(s):
    return "'" + str(s).replace("'", "''") + "'"


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--floors", default=FLOORS)
    ap.add_argument("--memory-limit", default="2GB")
    ap.add_argument("--threads", type=int, default=2)
    args = ap.parse_args()

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    for p in (SUMMARY, args.floors):
        if not os.path.exists(p):
            print(f"setup error: missing {p}")
            return 2

    floors = load_floors(args.floors)
    if not floors:
        print("setup error: no floor rows parsed")
        return 2

    import duckdb
    con = duckdb.connect()
    con.execute(f"SET memory_limit={sql_quote(args.memory_limit)}")
    con.execute(f"SET threads={args.threads}")
    S = f"read_parquet('{SUMMARY.replace(chr(92), '/')}')"

    active = [r for r in floors if r["active"]]
    skipped = [r for r in floors if not r["active"]]

    # One pass: pre-filter the summary to candidate rows (owner_family OR owner_norm in the
    # active family set), then aggregate per family via the same OR match.
    names = sorted({r["family"] for r in active})
    measured = {}
    if names:
        inlist = ", ".join(sql_quote(n) for n in names)
        vals = ", ".join(sql_quote(n) for n in names)
        q = f"""
        WITH cand AS (
          SELECT owner_family, owner_norm, parcels, states
          FROM {S}
          WHERE owner_family IN ({inlist}) OR owner_norm IN ({inlist})
        ),
        fam(family) AS (VALUES ({'), ('.join(sql_quote(n) for n in names)}))
        SELECT fam.family,
               coalesce(sum(c.parcels), 0)               AS parcels,
               coalesce(max(c.states), 0)                AS max_states,
               count(c.owner_norm)                       AS spellings
        FROM fam
        LEFT JOIN cand c
          ON c.owner_family = fam.family OR c.owner_norm = fam.family
        GROUP BY 1"""
        for fam, parcels, max_states, spellings in con.execute(q).fetchall():
            measured[fam] = (int(parcels), int(max_states), int(spellings))

    print("=== TIER A2: OWNER-FAMILY FLOORS (proposed) ===")
    print(f"  source: {os.path.relpath(SUMMARY, ROOT)}  |  floors: {os.path.relpath(args.floors, ROOT)}")
    print(f"  {len(active)} active, {len(skipped)} inactive(PENDING-REBUILD)\n")

    fails = []
    for r in sorted(active, key=lambda x: -x["min_parcels"]):
        fam = r["family"]
        parcels, max_states, spellings = measured.get(fam, (0, 0, 0))
        pok = parcels >= r["min_parcels"]
        sok = max_states >= r["min_states"]
        if pok and sok:
            print(f"  ok    {fam}: {parcels:,} parcels (>= {r['min_parcels']:,}), "
                  f"{max_states} states (>= {r['min_states']}), {spellings} spelling(s)")
        else:
            why = []
            if not pok:
                why.append(f"parcels {parcels:,} < floor {r['min_parcels']:,}")
            if not sok:
                why.append(f"max_states {max_states} < floor {r['min_states']}")
            msg = f"{fam}: " + "; ".join(why) + (" [family returned 0 — renamed/dropped in rebuild?]"
                                                 if parcels == 0 else "")
            fails.append(msg)
            print(f"  FAIL [A2] {msg}")

    for r in sorted(skipped, key=lambda x: x["family"]):
        print(f"  skip  {r['family']}: inactive until rebuild (measured_on={r['measured_on']}); "
              f"target floor {r['min_parcels']:,} parcels / {r['min_states']} states")

    print("\n=== SUMMARY ===")
    print(f"family floors: {len(active)} active checked, {len(skipped)} skipped (pending rebuild)")
    print(f"FAILURES: {len(fails)}")
    for m in fails:
        print(f"  FAIL [A2] {m}")
    if not fails:
        print("ALL ACTIVE FAMILY FLOORS PASSED")
    print("\nNote: SFR rows stay PENDING-REBUILD until the 282-rule registry lands (their family "
          "labels are near-empty today). Flip measured_on to a date to activate them in the "
          "rebuild commit, then re-run.")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
