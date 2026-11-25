"""Investable-universe mask — subtract the structurally-off-market parcel layers to isolate the
investable residual, GIS-masking style. Reusable filter (sql_mask) + a national characterization.

Masked (off-market):
  government            owner_type gov-federal/state/local
  conservation/nonprofit owner_type nonprofit-religious
  utility/infrastructure owner_type utility, or land_use transport/utilities
  farm/forest           land_use agriculture/forest

COARSE — and honest about why (see the coverage reality, 114.5M parcels):
  * owner-type masks are reliable (owner 95% covered).
  * land-use masks only cover ~38% standardized -> farm/forest is UNDER-counted (the other 62%
    of ag/forest land hides in the residual).
  * owner-occupied residential CANNOT be masked yet (mail_state ~0%) -> the residual OVER-counts
    homes people live in. True investable set is smaller, pending mail_state.
  * masks by OWNERSHIP STRUCTURE, not measured turnover (no sale data) -> "off-market" is inferred.

Usage: python src/investable_mask.py --report
"""
import argparse
import os
import sys

import duckdb

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import owner_type as ot  # noqa: E402

NORM = os.path.join(ROOT, "data", "parcels", "normalized", "*.parquet").replace(os.sep, "/")
REPORT = os.path.join(ROOT, "recon", "INVESTABLE_UNIVERSE.md")

# reason from an already-computed owner_type (`ot`) + land_use_category (`luc`)
REASON = """case
    when ot in ('gov-federal','gov-state','gov-local') then 'government'
    when ot = 'nonprofit-religious' then 'conservation/nonprofit'
    when ot = 'utility' or luc = 'transport/utilities' then 'utility/infrastructure'
    when luc = 'agriculture/forest' then 'farm/forest'
    else 'INVESTABLE' end"""


def report():
    con = duckdb.connect()
    con.execute("PRAGMA memory_limit='3GB'")
    con.execute("PRAGMA threads=2")
    con.execute("PRAGMA preserve_insertion_order=false")
    os.makedirs(os.path.join(ROOT, "data/parcels/owners/_spill"), exist_ok=True)
    con.execute(f"PRAGMA temp_directory='{os.path.join(ROOT, 'data/parcels/owners/_spill')}'")
    t = ot.sql_type("owner_norm", "owner_family")
    print("scanning normalized -> mask classification ...", flush=True)
    df = con.execute(f"""
        select reason, ot owner_type, count(*) parcels,
            sum(case when try_cast(acreage as double) between 0 and 5e6 then try_cast(acreage as double) end) acres,
            sum(try_cast(assessed_value as bigint)) val
        from (
            select ot, ({REASON}) reason, acreage, assessed_value
            from (select ({t}) ot, land_use_category luc, acreage, assessed_value
                  from read_parquet('{NORM}', union_by_name=true))
        ) group by 1, 2""").df()
    con.close()
    df["acres"] = df["acres"].fillna(0)
    df["val"] = df["val"].fillna(0)
    by_reason = df.groupby("reason").agg(parcels=("parcels", "sum"), acres=("acres", "sum"),
                                         val=("val", "sum")).reset_index()
    TP, TA, TV = by_reason["parcels"].sum(), by_reason["acres"].sum(), by_reason["val"].sum()
    order = ["government", "conservation/nonprofit", "utility/infrastructure", "farm/forest", "INVESTABLE"]
    by_reason["o"] = by_reason["reason"].map({r: i for i, r in enumerate(order)})
    by_reason = by_reason.sort_values("o")
    inv = df[df["reason"] == "INVESTABLE"].groupby("owner_type").agg(
        parcels=("parcels", "sum"), acres=("acres", "sum"), val=("val", "sum")).reset_index().sort_values("parcels", ascending=False)
    IP = inv["parcels"].sum()

    with open(REPORT, "w", encoding="utf-8") as f:
        f.write("# Investable-universe mask (coarse, national)\n\n")
        f.write(f"{int(TP):,} parcels / {int(TA):,} acres / ${TV/1e9:,.0f}B assessed. "
                "Subtract structurally-off-market layers -> investable residual. src/investable_mask.py.\n\n")
        f.write("## The mask\n\n| layer | parcels | % | acres | %acres | value $B |\n|---|--:|--:|--:|--:|--:|\n")
        for _, r in by_reason.iterrows():
            tag = "**" if r["reason"] == "INVESTABLE" else ""
            f.write(f"| {tag}{r['reason']}{tag} | {int(r['parcels']):,} | {100*r['parcels']/TP:.1f}% | "
                    f"{int(r['acres']):,} | {100*r['acres']/TA:.1f}% | {r['val']/1e9:,.0f} |\n")
        f.write(f"\n## Investable residual — composition by owner type ({int(IP):,} parcels)\n\n")
        f.write("| owner type | parcels | % of investable | value $B |\n|---|--:|--:|--:|\n")
        for _, r in inv.iterrows():
            f.write(f"| {r['owner_type']} | {int(r['parcels']):,} | {100*r['parcels']/IP:.1f}% | {r['val']/1e9:,.0f} |\n")
        f.write("\n## Honest limits (why it's COARSE)\n"
                "- **land_use only 38% standardized** -> farm/forest is UNDER-masked (much ag/forest land "
                "with unknown land-use stays in the residual).\n"
                "- **owner-occupied residential can't be masked yet** (mail_state ~0%) -> the residual "
                "OVER-counts homes people live in; the *true* investable set is smaller. mail_state "
                "(accruing on re-pulls) splits investor-held from owner-occupied.\n"
                "- masks by OWNERSHIP STRUCTURE, not measured turnover (no sale data) -> 'off-market' is inferred.\n")
    print(f"wrote {os.path.relpath(REPORT, ROOT)}")
    print("\n=== MASK (national) ===")
    for _, r in by_reason.iterrows():
        print(f"  {r['reason']:24} {int(r['parcels']):>13,} parcels ({100*r['parcels']/TP:>4.1f}%)  "
              f"{int(r['acres']):>13,} ac ({100*r['acres']/TA:>4.1f}%)")
    print(f"\n=== INVESTABLE residual: {int(IP):,} parcels ({100*IP/TP:.1f}% of all) ===")
    for _, r in inv.head(8).iterrows():
        print(f"  {r['owner_type']:22} {int(r['parcels']):>13,} ({100*r['parcels']/IP:>4.1f}%)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true")
    ap.parse_args()
    report()
