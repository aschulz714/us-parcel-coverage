"""mail_state from mail_zip — zip3 -> state derivation with a DATA-DRIVEN validation gate.

The USPS 3-digit zip prefix determines the state (a handful of known cross-border quirks:
733 Austin TX inside the OK band, 063 CT vs 06390 Fishers Island NY, 055 MA inside the VT
band, 834 ID at the WY edge...). Rather than trust the static table, validate_zip3() measures
per-zip3 agreement against the store itself — the ~20M rows that carry BOTH mail_state and
mail_zip — and returns only the zip3s whose observed agreement clears the threshold. Fill
passes must use that pruned map, so a wrong table entry can never write a wrong state.

Used by recon/mail_state_fill.py (backfill existing files) and normalize.py (derive at
normalize time when no state/CSZ field exists, so re-pulls keep the attribute).
"""
import re

# state -> list of inclusive zip3 ranges (best-effort USPS table; VALIDATED before use)
_RANGES = {
    "AL": [(350, 369)], "AK": [(995, 999)], "AZ": [(850, 865)], "AR": [(716, 729)],
    "CA": [(900, 961)], "CO": [(800, 816)], "CT": [(60, 69)], "DE": [(197, 199)],
    "DC": [(200, 205), (569, 569)], "FL": [(320, 349)], "GA": [(300, 319), (398, 399)],
    "HI": [(967, 968)], "ID": [(832, 838)], "IL": [(600, 629)], "IN": [(460, 479)],
    "IA": [(500, 528)], "KS": [(660, 679)], "KY": [(400, 427)], "LA": [(700, 714)],
    "ME": [(39, 49)], "MD": [(206, 219)], "MA": [(10, 27), (55, 55)], "MI": [(480, 499)],
    "MN": [(550, 567)], "MS": [(386, 397)], "MO": [(630, 658)], "MT": [(590, 599)],
    "NE": [(680, 693)], "NV": [(889, 898)], "NH": [(30, 38)], "NJ": [(70, 89)],
    "NM": [(870, 884)], "NY": [(4, 5), (100, 149)], "NC": [(270, 289)], "ND": [(580, 588)],
    "OH": [(430, 459)], "OK": [(730, 732), (734, 749)], "OR": [(970, 979)],
    "PA": [(150, 196)], "RI": [(28, 29)], "SC": [(290, 299)], "SD": [(570, 577)],
    "TN": [(370, 385)], "TX": [(733, 733), (750, 799), (885, 885)], "UT": [(840, 847)],
    "VT": [(50, 54), (56, 59)], "VA": [(201, 201), (220, 246)], "WA": [(980, 994)],
    "WV": [(247, 268)], "WI": [(530, 549)], "WY": [(820, 831)], "PR": [(6, 9)],
}

ZIP3_STATE = {}
for _st, _spans in _RANGES.items():
    for _lo, _hi in _spans:
        for _z in range(_lo, _hi + 1):
            ZIP3_STATE[f"{_z:03d}"] = _st

_ZIP5 = re.compile(r"(\d{5})")


def load_validated():
    """The committed, data-validated zip3->state map (data/zip3_state_validated.csv,
    written by validate_zip3 2026-07-10: 713 zip3s at >=98% observed agreement).
    Fill passes and normalize use THIS, never the raw table."""
    import csv
    import os
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "data", "zip3_state_validated.csv")
    with open(p, encoding="utf-8") as f:
        return {r["zip3"]: r["state"] for r in csv.DictReader(f)}


def zip3_series(zip_ser):
    """First 3 digits of the first 5-digit run in a mail_zip series (handles zip+4,
    float-shaped '92101.0', embedded text). Returns string Series with NA where no zip."""
    return zip_ser.astype("string").str.extract(_ZIP5, expand=False).str[:3]


def validate_zip3(threshold=0.98, min_rows=200, print_report=True):
    """Measure ZIP3_STATE against every normalized row that has BOTH mail_state and mail_zip.
    Returns {zip3: state} restricted to zip3s with >= threshold observed agreement on
    >= min_rows rows (plus zip3s absent from the data left out entirely — no evidence, no fill).
    """
    import os
    import duckdb
    here = os.path.dirname(os.path.abspath(__file__))
    pat = os.path.join(os.path.dirname(here), "data", "parcels", "normalized", "*.parquet").replace("\\", "/")
    con = duckdb.connect()
    con.execute("SET memory_limit='2GB'; SET threads=2")
    rows = con.execute(f"""
        SELECT substr(regexp_extract(mail_zip, '(\\d{{5}})', 1), 1, 3) AS z3,
               upper(trim(mail_state)) AS st, count(*) AS n
        FROM read_parquet('{pat}', union_by_name=true)
        WHERE mail_state IS NOT NULL AND mail_zip IS NOT NULL
          AND regexp_extract(mail_zip, '(\\d{{5}})', 1) <> ''
        GROUP BY 1, 2
    """).fetchall()
    by_z3 = {}
    for z3, st, n in rows:
        by_z3.setdefault(z3, {})[st] = by_z3.get(z3, {}).get(st, 0) + n
    ok, pruned = {}, []
    for z3, want in ZIP3_STATE.items():
        seen = by_z3.get(z3)
        if not seen or sum(seen.values()) < min_rows:
            pruned.append((z3, want, "no-evidence", 0, 0.0))
            continue
        total = sum(seen.values())
        agree = seen.get(want, 0) / total
        if agree >= threshold:
            ok[z3] = want
        else:
            top = max(seen, key=seen.get)
            pruned.append((z3, want, f"disagrees(top={top})", total, agree))
    if print_report:
        ev = [p for p in pruned if p[2] != "no-evidence"]
        print(f"zip3 validation: {len(ok)} accepted, {len(ev)} pruned on disagreement, "
              f"{len(pruned) - len(ev)} no-evidence (skipped)")
        for z3, want, why, n, agree in sorted(ev):
            print(f"  PRUNED {z3}->{want}: {why} agreement {agree:.1%} on {n:,} rows")
    return ok
