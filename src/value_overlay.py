"""Value overlay -- protect backfilled/recovered assessed_value from being wiped by re-normalize.

Counties whose value comes from a SEPARATE source than their geometry (the OH/TN/OR assessor-roll
backfills: TN's statewide OLG APPRAISAL, OH's county MKT_Total_Value layers, keyed by apn) lose that
value every time normalize rebuilds them from the value-less geometry source. This stores value in a
durable per-county overlay (apn -> value) that normalize RE-APPLIES automatically after each county,
so a re-pull no longer destroys value. Fills only NULL/<=0 cells -- never overwrites a positive value
the geometry source produced itself.

  python src/value_overlay.py capture --fips 47001            # snapshot current value -> overlay
  python src/value_overlay.py capture --state TN --source tn_olg
  python src/value_overlay.py apply --fips 47001              # re-apply onto normalized (standalone)
  python src/value_overlay.py list
"""
import argparse
import glob
import os
import sys

import pandas as pd

from atomic_io import atomic_replace_writer
from source_account_roles import require_safe_overlay

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NORM = os.path.join(ROOT, "data", "parcels", "normalized")
OVERLAY = os.path.join(ROOT, "data", "parcels", "value_overlay")
VALUE_COLS = ["assessed_value", "land_value", "improvement_value"]


def overlay_path(fips):
    return os.path.join(OVERLAY, f"{fips}.parquet")


def _norm_file(fips):
    fs = glob.glob(os.path.join(NORM, f"{fips}_*.parquet"))
    return fs[0] if fs else None


def capture(fips, source="backfill"):
    """Snapshot the current normalized county's positive value into its overlay (apn keyed).
    Column-projected pyarrow read — geometry is never touched."""
    require_safe_overlay(fips)
    f = _norm_file(fips)
    if not f:
        print(f"  {fips}: no normalized parquet"); return 0
    import pyarrow.parquet as pq
    names = pq.ParquetFile(f).schema_arrow.names
    if "apn" not in names or "assessed_value" not in names:
        print(f"  {fips}: missing apn/assessed_value"); return 0
    df = pq.read_table(f, columns=["apn"] + [c for c in VALUE_COLS if c in names]).to_pandas()
    val = pd.to_numeric(df["assessed_value"], errors="coerce")
    keep = df.loc[val > 0, ["apn"] + [c for c in VALUE_COLS if c in df.columns]].copy()
    keep["apn"] = keep["apn"].astype(str)
    keep = keep[keep["apn"].str.strip().ne("") & keep["apn"].str.lower().ne("none")]
    keep = keep.drop_duplicates("apn", keep="first")
    if not len(keep):
        print(f"  {fips}: 0 valued parcels, nothing to capture"); return 0
    for c in VALUE_COLS:
        if c in keep.columns:
            keep[c] = pd.to_numeric(keep[c], errors="coerce")
    keep["value_source"] = source
    os.makedirs(OVERLAY, exist_ok=True)
    atomic_replace_writer(
        overlay_path(fips), lambda temp: keep.to_parquet(temp, index=False, compression="zstd"))
    print(f"  {fips}: captured {len(keep):,} valued parcels -> overlay [{source}]")
    return len(keep)


def apply_to(df, fips):
    """Fill NULL/<=0 assessed_value (+ land/improvement) from the overlay, matched on apn. No-op when
    no overlay exists. NEVER overwrites an existing positive value. Returns the (same) df.
    Called by normalize after building each county so re-pulls auto-restore backfilled value."""
    p = overlay_path(fips)
    if not os.path.exists(p):
        return df
    require_safe_overlay(fips)
    if "apn" not in df.columns:
        return df
    ov = pd.read_parquet(p).drop_duplicates("apn")
    ov["apn"] = ov["apn"].astype(str)
    ov = ov.set_index("apn")
    key = df["apn"].astype(str)
    for col in VALUE_COLS:
        if col not in df.columns or col not in ov.columns:
            continue
        cur = pd.to_numeric(df[col], errors="coerce")
        need = cur.isna() | (cur <= 0)
        if not need.any():
            continue
        mapped = key.map(ov[col])
        # substitute ONLY where the overlay has a value: a NULL/$0 row whose apn is not in
        # the overlay must stay as-is, not be nulled by the unmatched map (genuine $0s were
        # silently becoming NULL in every overlay county on each re-normalize)
        df[col] = cur.mask(need & mapped.notna(), mapped)
    return df


def apply_standalone(fips):
    """Re-apply overlay onto an existing normalized parquet on disk (outside the normalize run).
    pyarrow column swap (2026-07-10): only apn+value columns are materialized; geometry stays
    opaque bytes and every write is zstd (the old geopandas path silently re-wrote whole
    counties as snappy)."""
    f = _norm_file(fips)
    if not f or not os.path.exists(overlay_path(fips)):
        print(f"  {fips}: no normalized parquet or no overlay"); return
    import pyarrow.parquet as pq
    from parquet_cols import write_columns
    table = pq.read_table(f)
    if "apn" not in table.column_names or "assessed_value" not in table.column_names:
        print(f"  {fips}: missing apn/assessed_value"); return
    cols = ["apn"] + [c for c in VALUE_COLS if c in table.column_names]
    df = table.select(cols).to_pandas()
    before = int((pd.to_numeric(df["assessed_value"], errors="coerce") > 0).sum())
    df = apply_to(df, fips)
    after = int((pd.to_numeric(df["assessed_value"], errors="coerce") > 0).sum())
    if after != before:
        # canonical whole-dollar nullable Int64 (see normalize.py) — apply_to's mask can
        # upcast to float, which would drift the on-disk type
        newcols = {c: pd.to_numeric(df[c], errors="coerce").round().astype("Int64")
                   for c in VALUE_COLS if c in df.columns}
        write_columns(f, table, newcols)
    print(f"  {fips}: value {before:,} -> {after:,} (+{after-before:,})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["capture", "apply", "list"])
    ap.add_argument("--fips", nargs="*")
    ap.add_argument("--state")
    ap.add_argument("--source", default="backfill")
    a = ap.parse_args()

    if a.cmd == "list":
        ps = sorted(glob.glob(os.path.join(OVERLAY, "*.parquet")))
        tot = 0
        for p in ps:
            n = pd.read_parquet(p, columns=["apn"]).shape[0]
            tot += n
            print(f"  {os.path.basename(p)[:-8]}: {n:,}")
        print(f"{len(ps)} overlays | {tot:,} protected parcels")
        return

    fipslist = list(a.fips or [])
    if a.state:
        # derive the fips2 prefix from the crosswalk — works for every state, no hand-kept map
        import csv
        xw = os.path.join(os.path.dirname(os.path.abspath(__file__)), "county_methods.csv")
        with open(xw, encoding="utf-8") as f:
            prefixes = {r["fips"][:2] for r in csv.DictReader(f) if r["state"] == a.state.upper()}
        if not prefixes:
            sys.exit(f"unknown state {a.state!r} in county_methods.csv")
        pre = prefixes.pop()
        fipslist += [os.path.basename(f).split("_")[0]
                     for f in glob.glob(os.path.join(NORM, f"{pre}*.parquet"))]
    fipslist = sorted(set(fipslist))
    if not fipslist:
        print("no fips (use --fips or --state)"); return
    total = 0
    for fp in fipslist:
        if a.cmd == "capture":
            total += capture(fp, a.source)
        else:
            apply_standalone(fp)
    if a.cmd == "capture":
        print(f"TOTAL captured: {total:,} across {len(fipslist)} counties")


if __name__ == "__main__":
    main()
