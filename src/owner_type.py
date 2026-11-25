"""Owner-TYPE taxonomy — classify every owner into WHAT KIND it is (orthogonal to identity in
owner_entity / owner_family). Backbone for owner analytics + the future distress layer.

Types: individual, corporate-investor, institutional-sfr, homebuilder, bank-reo, gov-federal,
gov-state, gov-local, land-bank, estate, trust, utility, nonprofit-religious.

Signal = the owner NAME (+ owner_family's null=individual / non-null=org prior). Implemented as a
DuckDB SQL CASE so it classifies ~60M owners in one vectorized pass (no per-row Python, low memory).
Reuses the federal/state/local rules from major_owners (single source of truth for government).

Usage:
  python src/owner_type.py --report        # national breakdown by type (parcels / value / owners)
  python src/owner_type.py --write         # also write owner_type.parquet (owner_norm -> type)
"""
import argparse
import os

import duckdb

import major_owners as mo  # reuse FED/STATE/LOCAL rules

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OWN = os.path.join(ROOT, "data", "parcels", "owners")
SUMMARY = os.path.join(OWN, "owner_summary.parquet")
OUT = os.path.join(OWN, "owner_type.parquet")

P_ESTATE = r"(ESTATE OF|HEIRS OF| HEIRS\b|LIFE ESTATE|\bEST OF\b)"
P_BANKREO = (r"(FANNIE MAE|FREDDIE MAC|FEDERAL NATIONAL MORTGAGE|FEDERAL HOME LOAN MORTGAGE|"
             r"GOVERNMENT NATIONAL MORTGAGE|SECRETARY OF (HOUSING|VETERANS)|\bHUD\b| AS TRUSTEE|"
             r"TRUSTEE FOR| TRUSTEE\b|BANK OF AMERICA|WELLS FARGO|\bUS BANK|DEUTSCHE BANK|JPMORGAN|"
             r"CITIBANK|\bREO\b|MORTGAGE (CORP|CO\b|LLC|ASSN|ASSOCIATION)|FORECLOSURE)")
P_BUILDER = (r"(D R HORTON|DR HORTON|LENNAR|PULTE|MERITAGE|KB HOME|TAYLOR MORRISON|TRI ?POINTE|"
             r"CENTURY COMMUNITIES|DREAM FINDERS|LGI HOMES|RICHMOND AMERICAN|BEAZER|PERRY HOMES|"
             r"MILLROSE|STARLIGHT HOMES|BRIGHTLAND|HIGHLAND HOMES|HOMES OF |HOMEBUILD|M I HOMES|MI HOMES)")
P_SFR = (r"(PROGRESS RESIDENTIAL|INVITATION HOMES|AMERICAN HOMES 4 RENT|\bAMH\b|FIRSTKEY|TRICON|"
         r"VINEBROOK|AMHERST|FYR SFR|HOME PARTNERS|MAYMONT|CERBERUS|\bSFR\b|HOMES 4 RENT| BORROWER\b)")
P_UTILITY = (r"\b(ELECTRIC|POWER CO|POWER COMPANY|ENERGY|PIPELINE|NATURAL GAS|GAS CO|TELEPHONE|"
             r"COMMUNICATIONS|UTILIT|WATERWORKS|WATER CO|RAILROAD|RAILWAY|TRANSMISSION|"
             r"COOPERATIVE|CO ?OP)\b")
P_NONPROFIT = (r"\b(CHURCH|MINISTR|DIOCESE|TEMPLE|SYNAGOGUE|MOSQUE|CONGREGATION|FOUNDATION|"
               r"HABITAT FOR HUMANITY|UNIVERSITY|COLLEGE|HOSPITAL|YMCA|SALVATION ARMY|CATHOLIC|"
               r"BAPTIST|METHODIST|LUTHERAN|PRESBYTERIAN|ASSEMBLY OF GOD|NONPROFIT|CHARIT)\b")
P_LANDBANK = r"(LAND ?BANK|LAND REUTIL)"
P_TRUST = r"(\bTRUST\b|\bLIV(ING)? TR\b|FAMILY TR\b|\bREV(OCABLE)? TR\b)"
P_CORP = (r"\b(LLC|L L C|INC|CORP|CORPORATION|\bLP\b|L P|LLP|LTD|COMPANY|\bCO\b|HOLDINGS|PROPERT|"
          r"INVESTMENT|CAPITAL|PARTNERS|GROUP|ENTERPRISE|REALTY|VENTURE|MANAGEMENT|ASSOCIATES|"
          r"DEVELOPMENT)\b")


def sql_type(name="owner_norm", fam="owner_family"):
    u = f"upper(trim({name}))"
    fed, state = mo.sql_fed_state(name)
    return f"""case
      when regexp_matches({u}, '{P_ESTATE}') then 'estate'
      when {fed} then 'gov-federal'
      when {state} then 'gov-state'
      when regexp_matches({u}, '{P_LANDBANK}') then 'land-bank'
      when regexp_matches({u}, '{mo.P_LOCAL}') then 'gov-local'
      when regexp_matches({u}, '{P_BANKREO}') then 'bank-reo'
      when regexp_matches({u}, '{P_BUILDER}') then 'homebuilder'
      when regexp_matches({u}, '{P_SFR}') then 'institutional-sfr'
      when regexp_matches({u}, '{P_UTILITY}') then 'utility'
      when regexp_matches({u}, '{P_NONPROFIT}') then 'nonprofit-religious'
      when regexp_matches({u}, '{P_TRUST}') then 'trust'
      when regexp_matches({u}, '{P_CORP}') or {fam} is not null then 'corporate-investor'
      else 'individual' end"""


def report(write):
    con = duckdb.connect()
    con.execute("PRAGMA memory_limit='4GB'")
    con.execute("PRAGMA threads=3")
    con.execute("PRAGMA preserve_insertion_order=false")
    t = sql_type("owner_norm", "owner_family")
    # value column = total_value_dedup (stamped-total artifacts collapsed; see docs/VALUE_QUALITY.md)
    df = con.execute(f"""select ({t}) owner_type, count(*) owners, sum(parcels) parcels,
        sum(total_value_dedup) val from read_parquet('{SUMMARY}') group by 1 order by parcels desc""").df()
    tot_p = df["parcels"].sum()
    tot_o = df["owners"].sum()
    print(f"national owner-type breakdown ({int(tot_o):,} distinct owners, {int(tot_p):,} parcels):\n")
    print(f"{'type':22} {'parcels':>13} {'%':>6} {'owners':>13} {'value $B*':>10}  (*de-duped)")
    for _, r in df.iterrows():
        print(f"{r['owner_type']:22} {int(r['parcels']):>13,} {100*r['parcels']/tot_p:>5.1f}% "
              f"{int(r['owners']):>13,} {(r['val'] or 0)/1e9:>10.1f}")
    if write:
        con.execute(f"""copy (select owner_norm, ({t}) owner_type
            from read_parquet('{SUMMARY}')) to '{OUT}' (format parquet)""")
        print(f"\nwrote {os.path.relpath(OUT, ROOT)} (owner_norm -> owner_type)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--write", action="store_true")
    a = ap.parse_args()
    report(a.write)
