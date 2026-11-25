"""Reclaim disk: delete raw county parquets whose NORMALIZED counterpart is verified.

Raw is re-pullable from county servers (that's the manifest + refresh design), so once a
county is normalized, raw is redundant. Safety rules — a raw file is deleted ONLY if:
  1. the same-named normalized parquet exists, and
  2. normalized row count >= 95% of raw row count (counts read from parquet metadata,
     nothing loaded into memory).
Counties mid-ingest or unnormalized are skipped automatically (rule 1).
NOTE: re-running normalize.py for a cleaned county requires re-ingesting it first.

Usage:
    python src/cleanup_raw.py            # clean everything eligible
    python src/cleanup_raw.py --state WA # one state
    python src/cleanup_raw.py --dry-run  # report only, delete nothing
"""
import argparse
import glob
import os

import pyarrow.parquet as pq

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW = os.path.join(HERE, "data", "parcels", "raw")
NORM = os.path.join(HERE, "data", "parcels", "normalized")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", help="2-digit FIPS prefix or 2-letter handled upstream", default=None)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    state = args.state
    if state and not state.isdigit():
        # accept 2-letter codes (FL -> 12) — and NEVER silently widen scope: the original
        # fall-through to '*' once deleted every state when given 'FL'
        import csv
        xw = os.path.join(os.path.dirname(os.path.abspath(__file__)), "county_methods.csv")
        with open(xw, encoding="utf-8") as f:
            matches = {r["fips"][:2] for r in csv.DictReader(f) if r["state"] == state.upper()}
        if not matches:
            raise SystemExit(f"unknown state {state!r} — refusing to run unscoped")
        state = matches.pop()
    pattern = f"{state}*" if state else "*"
    raws = sorted(glob.glob(os.path.join(RAW, f"{pattern}.parquet")))
    freed, kept, deleted = 0, 0, 0
    for rp in raws:
        name = os.path.basename(rp)
        np_ = os.path.join(NORM, name)
        if not os.path.exists(np_):
            kept += 1
            continue
        try:
            raw_rows = pq.ParquetFile(rp).metadata.num_rows
            norm_rows = pq.ParquetFile(np_).metadata.num_rows
        except Exception as e:
            print(f"  SKIP {name}: unreadable metadata ({e})")
            kept += 1
            continue
        if raw_rows and norm_rows < raw_rows * 0.95:
            print(f"  SKIP {name}: normalized {norm_rows:,} < 95% of raw {raw_rows:,}")
            kept += 1
            continue
        size = os.path.getsize(rp)
        if args.dry_run:
            print(f"  would delete {name} ({size/1e6:,.0f} MB, norm {norm_rows:,}/{raw_rows:,})")
        else:
            os.remove(rp)
        freed += size
        deleted += 1
    verb = "would free" if args.dry_run else "freed"
    print(f"{'DRY RUN: ' if args.dry_run else ''}{deleted} raw files deleted, "
          f"{kept} kept/skipped, {verb} {freed/1e9:.2f} GB")


if __name__ == "__main__":
    main()
