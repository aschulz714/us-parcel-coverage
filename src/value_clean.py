"""Null SENTINEL assessed_value values — placeholder garbage that poisons value-based ranking.

assessed_value is ~99.9% sound (national median ~$212k). The damage is a sparse tail of PLACEHOLDER
values that some assessors stamp in lieu of a real number, which then dominate any value ranking and
explode owner/portfolio sums (e.g. Tangipahoa Parish LA stamped 99,999,900 / 999,999,999 on ~24k
parcels — that's the entire "$77B DR Horton" artifact). This nulls those placeholders (the honest
representation: we don't know the real value) while preserving every legitimate value, including the
genuinely-huge ones (Intel's $21B fab, naval bases) and the "stamped-total" installation values that
are real-but-misdistributed (those are a separate, aggregation-layer concern — see docs/VALUE_QUALITY.md).

A value is a sentinel when EITHER:
  - it is all-9s, >=7 digits (9999999 ... 99999999999) — unambiguous, nulled anywhere; OR
  - it matches the placeholder shape 9{6,}0*  (e.g. 99999900) AND occurs on >100 parcels in the SAME
    county (a one-off real $99,999,900 is spared; a bulk-stamped placeholder is not).

pyarrow I/O (geometry opaque, preserved). Only files that actually contain a sentinel are rewritten.

Usage:
  python src/value_clean.py --all
  python src/value_clean.py --fips 22105
"""
import argparse, glob, json, os, re
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from atomic_io import atomic_replace_writer

HERE = os.path.dirname(__file__)
ROOT = os.path.dirname(HERE)
NORM = os.path.join(ROOT, "data", "parcels", "normalized")

ALL9 = {9999999, 99999999, 999999999, 9999999999, 99999999999}   # all-9s, >=7 digits
PLACEHOLDER = re.compile(r"^9{6,}0*$")                            # 9{6,}0*  (e.g. 99999900)
BULK_MIN = 100                                                    # placeholder must be bulk to null


def _meta_keep(meta):
    return meta  # columns unchanged (we only edit values in an existing column) -> metadata intact


def clean_file(path):
    t = pq.read_table(path)
    if "assessed_value" not in t.column_names:
        return 0
    v = t.column("assessed_value").to_pandas()
    num = pd.to_numeric(v, errors="coerce")
    sentinel = num.isin(ALL9)
    # bulk placeholders (9{6,}0*) appearing >100x in THIS county
    vc = num.value_counts()
    bulk = {val for val, c in vc.items()
            if c > BULK_MIN and PLACEHOLDER.match(str(int(val))) and val not in ALL9}
    if bulk:
        sentinel = sentinel | num.isin(bulk)
    n = int(sentinel.sum())
    if not n:
        return 0
    cleaned = v.where(~sentinel.values, other=None)
    arr = pa.array(cleaned.astype(object).where(pd.notna(cleaned), None), type=t.schema.field("assessed_value").type)
    idx = t.column_names.index("assessed_value")
    t = t.set_column(idx, "assessed_value", arr)
    atomic_replace_writer(
        path, lambda temp: pq.write_table(t, temp, compression="zstd"))
    bulkstr = (" + bulk " + ",".join(f"{int(b):,}" for b in sorted(bulk))) if bulk else ""
    print(f"  {os.path.basename(path)}: nulled {n:,} sentinel values{bulkstr}", flush=True)
    return n


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--fips", nargs="*")
    a = ap.parse_args()
    if a.fips:
        files = [g for f in a.fips for g in glob.glob(os.path.join(NORM, f"{f}*.parquet"))]
    elif a.all:
        files = sorted(glob.glob(os.path.join(NORM, "*.parquet")))
    else:
        raise SystemExit("--all or --fips ...")
    print(f"value_clean: scanning {len(files)} counties for sentinel assessed_value...")
    total = cos = 0
    for p in files:
        n = clean_file(p)
        if n:
            cos += 1
            total += n
    print(f"done: nulled {total:,} sentinel values across {cos} counties")


if __name__ == "__main__":
    main()
