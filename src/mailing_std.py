"""Add a cleaned `mailing_norm` column to the normalized parcels — the mailing-address analog of
owner_norm. A CLEANED COLUMN, not a merge: it standardizes the address so matching is possible,
but the matching/merging decision stays a separate, deliberate step (so shared institutional /
tax c-o addresses don't inadvertently fuse unrelated owners).

Normalization (validated to collapse 'PO BOX 4090' / 'P O BOX 4090' / 'P.O. BOX 4090, SCOTTSDALE,
AZ 85261-4090' -> the single key 'PO BOX 4090'):
  - upper, strip punctuation -> space, collapse whitespace
  - 'P O BOX' / 'P.O. BOX' -> 'PO BOX'
  - PO boxes -> canonical 'PO BOX <number>' (drop redundant city/state/zip that splits the key)
  - street addresses -> standardize type + directional tokens (STREET->ST, NORTH->N, ...)

Per-county rewrite (gpd read -> add column -> write), preserving geometry; OOM-proof (pure per-row,
no cross-county aggregation). mailing_norm is regenerable from mailing_address, so the R2 backup
need not be re-pushed urgently. mailing_norm is NOT a tile field, so no re-tiling is needed.

Usage:
  python src/mailing_std.py --state WA      # one state
  python src/mailing_std.py --fips 53033    # specific counties
  python src/mailing_std.py --all           # every normalized county
"""
import argparse, glob, json, os

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from atomic_io import atomic_replace_writer

HERE = os.path.dirname(__file__)
ROOT = os.path.dirname(HERE)
NORM = os.path.join(ROOT, "data", "parcels", "normalized")

# street type + directional standardization (whole-word, space-padded). Canonical = abbreviation.
_STREET = [(" STREET ", " ST "), (" AVENUE ", " AVE "), (" BOULEVARD ", " BLVD "),
           (" ROAD ", " RD "), (" DRIVE ", " DR "), (" LANE ", " LN "), (" COURT ", " CT "),
           (" PLACE ", " PL "), (" CIRCLE ", " CIR "), (" HIGHWAY ", " HWY "), (" PARKWAY ", " PKWY "),
           (" NORTH ", " N "), (" SOUTH ", " S "), (" EAST ", " E "), (" WEST ", " W "),
           (" NORTHEAST ", " NE "), (" NORTHWEST ", " NW "), (" SOUTHEAST ", " SE "), (" SOUTHWEST ", " SW ")]


def mailing_normalize(s, mail_zip=None, mail_city=None, mail_state=None):
    """pandas Series[str|NA] -> Series[str|NA] cleaned mailing key (NA where blank/missing).
    PO boxes -> 'PO BOX <box> <zip5>' (zip5 KEPT so Scottsdale's box 4090 != Denver's box 4090 --
    dropping it conflates unrelated owners across cities).

    v2 (2026-07-10): most sources put ONLY the street line in mailing_address — measured 79.9%
    of national keys carried no city/zip, so '123 MAIN ST' collided across every city in the
    country and cross-county matching had to stay anchor-only. Keys that lack a zip are now
    COMPLETED from the county's own mail_zip / mail_city+mail_state columns (zip5 preferred,
    city+state fallback). Same-address keys stay identical across counties whenever the same
    completion tier is available; a key that can't be completed stays bare (match-suppressed
    downstream, never guessed)."""
    x = s.fillna("").astype(str).str.upper()
    x = x.str.replace(r"[^A-Z0-9 ]", " ", regex=True).str.replace(r" +", " ", regex=True).str.strip()
    x = x.str.replace(r"(^| )P O BOX ", r"\1PO BOX ", regex=True)  # P O BOX / P.O. BOX -> PO BOX
    box = x.str.extract(r"PO BOX ([0-9]+)", expand=False)          # PO box number, if any
    zip5 = x.str.extract(r".*\b([0-9]{5})\b", expand=False)        # last 5-digit token = zip5
    pobox = "PO BOX " + box.fillna("")
    has_zip = zip5.notna() & (zip5 != box)                         # append zip5 unless it IS the box
    pobox = pobox.mask(has_zip, pobox + " " + zip5.fillna(""))
    street = " " + x + " "
    for a, b in _STREET:
        street = street.str.replace(a, b, regex=False)
    street = street.str.replace(r" +", " ", regex=True).str.strip()
    out = street.mask(box.notna(), pobox)                         # PO box -> canonical key w/ zip5
    out = out.replace("", pd.NA)

    # ---- v2 completion: append zip5 (else CITY ST) to keys that carry no zip ----
    bare = out.notna() & ~out.str.contains(r"\b[0-9]{5}$", regex=True, na=False)
    if mail_zip is not None and bare.any():
        zc = mail_zip.astype("string").str.extract(r"([0-9]{5})", expand=False)
        fix = bare & zc.notna()
        out = out.mask(fix, out + " " + zc)
        bare = bare & ~fix
    if mail_city is not None and mail_state is not None and bare.any():
        cc = mail_city.fillna("").astype(str).str.upper() \
            .str.replace(r"[^A-Z ]", " ", regex=True).str.replace(r" +", " ", regex=True).str.strip()
        sc = mail_state.fillna("").astype(str).str.upper().str.strip()
        fix = bare & cc.ne("") & sc.str.len().eq(2)
        out = out.mask(fix, out + " " + cc + " " + sc)
    return out


def _meta_with_col(meta, col):
    """Add `col` to the parquet's pandas column metadata so geopandas/pandas surface it on read,
    while preserving the geoparquet 'geo' metadata (geometry stays intact)."""
    if not meta or b"pandas" not in meta:
        return meta
    pm = json.loads(meta[b"pandas"])
    if col not in [c["name"] for c in pm.get("columns", [])]:
        pm["columns"].append({"name": col, "field_name": col, "pandas_type": "unicode",
                              "numpy_type": "object", "metadata": None})
    out = dict(meta)
    out[b"pandas"] = json.dumps(pm).encode()
    return out


def apply_files(files):
    """pyarrow column-add: geometry stays as opaque bytes (NO shapely round-trip -> ~10x faster
    than geopandas), only the mailing_address string column is touched. Geoparquet metadata preserved."""
    for p in files:
        t = pq.read_table(p)
        if "mailing_address" not in t.column_names:
            print(f"  {os.path.basename(p)}: no mailing_address, skipped", flush=True)
            continue
        aux = {c: t.column(c).to_pandas() if c in t.column_names else None
               for c in ("mail_zip", "mail_city", "mail_state")}
        mn = mailing_normalize(t.column("mailing_address").to_pandas(),
                               mail_zip=aux["mail_zip"], mail_city=aux["mail_city"],
                               mail_state=aux["mail_state"])
        arr = pa.array(mn, type=pa.string())
        if "mailing_norm" in t.column_names:
            t = t.drop(["mailing_norm"])               # idempotent re-run
        t = t.append_column("mailing_norm", arr)
        t = t.replace_schema_metadata(_meta_with_col(t.schema.metadata, "mailing_norm"))
        atomic_replace_writer(
            p, lambda temp: pq.write_table(t, temp, compression="zstd"))
        print(f"  {os.path.basename(p)}: mailing_norm {mn.notna().mean():.0%}", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--state")
    ap.add_argument("--fips", nargs="*")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--missing-only", action="store_true",
                    help="only files that lack mailing_norm (drift backfill: 291 files / 8.2M rows "
                         "on 2026-09-14 from sweeps that never ran this step)")
    a = ap.parse_args()
    if a.state:
        files = [f for f in sorted(glob.glob(os.path.join(NORM, "*.parquet")))
                 if _state_of(f) == a.state.upper()]
    elif a.fips:
        files = [g for f in a.fips for g in glob.glob(os.path.join(NORM, f"{f}*.parquet"))]
    elif a.all:
        files = sorted(glob.glob(os.path.join(NORM, "*.parquet")))
    else:
        raise SystemExit("--state XX, --fips ..., or --all")
    if a.missing_only:
        files = [f for f in files if "mailing_norm" not in pq.read_schema(f).names]
    print(f"mailing_std: {len(files)} counties")
    apply_files(files)


def _state_of(path):
    import csv
    if not hasattr(_state_of, "_m"):
        with open(os.path.join(HERE, "county_methods.csv"), encoding="utf-8") as f:
            _state_of._m = {r["fips"][:2]: r["state"] for r in csv.DictReader(f)}
    return _state_of._m.get(os.path.basename(path)[:2], "")


if __name__ == "__main__":
    main()
