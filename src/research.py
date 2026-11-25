"""Parcel research queries over the normalized local dataset (DuckDB on GeoParquet).

This is the payoff of the ingest->normalize pipeline: questions the live point resolver
can't answer — "every parcel this owner holds", value filters, county stats — answered
locally in milliseconds, across every ingested county at once.

Usage:
  python src/research.py owner "AUGUSTUS LLC"            # substring owner search
  python src/research.py owner "WEYERHAEUSER" --min-value 1000000 --limit 30
  python src/research.py apn 140850020000                # APN lookup (prefix ok)
  python src/research.py top-owners --state WA --limit 25
  python src/research.py stats                           # per-county coverage + vintage
"""
import argparse, glob, os, sys

import duckdb

HERE = os.path.dirname(__file__)
ROOT = os.path.dirname(HERE)
NORM = os.path.join(ROOT, "data", "parcels", "normalized")
COLS = ("state, county_fips, county_name, apn, owner, owner_norm, owner_family, "
        "situs_address, mailing_address, assessed_value, land_use, acreage, source_vintage")


def _con():
    if not glob.glob(os.path.join(NORM, "*.parquet")):
        sys.exit("no normalized parcels — run src/ingest.py then src/normalize.py first")
    con = duckdb.connect()
    pattern = os.path.join(NORM, "*.parquet").replace("\\", "/")
    con.execute(f"CREATE VIEW parcels AS SELECT {COLS} FROM read_parquet('{pattern}', union_by_name=true)")
    return con


def _show(df):
    if df.empty:
        print("no matches")
        return
    import pandas as pd
    pd.set_option("display.max_rows", 500)
    pd.set_option("display.width", 200)
    pd.set_option("display.max_colwidth", 40)
    print(df.to_string(index=False))


def cmd_owner(args):
    con = _con()
    where, params = ["owner ILIKE ?"], [f"%{args.pattern}%"]
    if args.state:
        where.append("state = ?"); params.append(args.state.upper())
    if args.min_value:
        where.append("assessed_value >= ?"); params.append(args.min_value)
    w = " AND ".join(where)
    df = con.execute(
        f"SELECT state, county_name, apn, owner, situs_address, assessed_value "
        f"FROM parcels WHERE {w} ORDER BY assessed_value DESC NULLS LAST LIMIT ?",
        params + [args.limit]).df()
    _show(df)
    tot = con.execute(f"SELECT count(*) n, sum(assessed_value) v, count(DISTINCT county_fips) c "
                      f"FROM parcels WHERE {w}", params).fetchone()
    print(f"\nTOTAL: {tot[0]} parcels across {tot[2]} counties, "
          f"assessed value ${tot[1]:,.0f}" if tot[1] else f"\nTOTAL: {tot[0]} parcels across {tot[2]} counties")


def cmd_apn(args):
    df = _con().execute(
        f"SELECT {COLS} FROM parcels WHERE apn LIKE ? LIMIT 50", [args.apn + "%"]).df()
    _show(df.T if len(df) == 1 else df)


def cmd_top_owners(args):
    con = _con()
    # --family rolls distinct legal entities into their common-ownership family;
    # --canonical (default) groups by the standardized name; --raw by the original spelling
    key = ("owner_family" if args.family else
           "owner" if args.raw else "coalesce(owner_norm, owner)")
    # NOT LIKE '[%' drops sentinel markers (e.g. Clark WA's '[SUPPRESSED - RCW...]')
    where = (f"WHERE {key} IS NOT NULL AND {key} NOT LIKE '[%'"
             + (" AND state = ?" if args.state else ""))
    params = [args.state.upper()] if args.state else []
    extra = ", count(DISTINCT owner_norm) entities" if args.family else ""
    df = con.execute(
        f"SELECT {key} AS owner, count(*) parcels, count(DISTINCT county_fips) counties, "
        f"count(DISTINCT state) states{extra}, sum(assessed_value)::BIGINT total_value "
        f"FROM parcels {where} GROUP BY 1 ORDER BY parcels DESC LIMIT ?",
        params + [args.limit]).df()
    _show(df)


def cmd_stats(args):
    df = _con().execute(
        "SELECT state, county_fips, county_name, count(*) parcels, "
        "round(100*avg(CASE WHEN owner IS NOT NULL THEN 1 ELSE 0 END)) pct_owner, "
        "round(100*avg(CASE WHEN assessed_value IS NOT NULL THEN 1 ELSE 0 END)) pct_value, "
        "max(source_vintage) vintage "
        "FROM parcels GROUP BY 1,2,3 ORDER BY 1,3").df()
    _show(df)
    print(f"\n{df['parcels'].sum():,} parcels in {len(df)} counties")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("owner", help="search parcels by owner substring")
    p.add_argument("pattern")
    p.add_argument("--state")
    p.add_argument("--min-value", type=float)
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(fn=cmd_owner)
    p = sub.add_parser("apn", help="look up a parcel by APN (prefix ok)")
    p.add_argument("apn")
    p.set_defaults(fn=cmd_apn)
    p = sub.add_parser("top-owners", help="largest owners by parcel count")
    p.add_argument("--state")
    p.add_argument("--limit", type=int, default=25)
    p.add_argument("--family", action="store_true",
                   help="roll up to common-ownership families (Weyerhaeuser entities as one)")
    p.add_argument("--raw", action="store_true", help="group by the original spelling")
    p.set_defaults(fn=cmd_top_owners)
    p = sub.add_parser("stats", help="per-county dataset coverage + vintage")
    p.set_defaults(fn=cmd_stats)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
