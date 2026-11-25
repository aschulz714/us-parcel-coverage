"""Smoke test: Georgia per-county owner sweep (2026-06-07), metro + rural + edge cases.
Run: python src/test_ga_sweep.py
Covers new arcgis wins across the state, the Barrow co-owner (lastname|address1) join,
the GMASS-consultant-hosted county (Taylor), and a web-app-only county that MUST route
honestly (status != resolved) rather than falsely claim an owner."""
import os, sys
sys.path.insert(0, os.path.dirname(__file__))
from resolve import resolve

TESTS = [
    # metro / suburban
    ("Forsyth - Cumming courthouse (13117)",     dict(lat=34.2073, lon=-84.1402)),
    ("Bartow - Cartersville (13015, self-host)", dict(lat=34.1654, lon=-84.8003)),
    ("Cherokee - Canton (13057)",                dict(lat=34.2369, lon=-84.4908)),
    ("Hall - Gainesville (13139)",               dict(lat=34.2979, lon=-83.8241)),
    ("Barrow - Winder (13013, lastname|addr1)",  dict(lat=33.9926, lon=-83.7202)),
    ("Bulloch - Statesboro (13031)",             dict(lat=32.4488, lon=-81.7832)),
    # rural
    ("Ben Hill - Fitzgerald (13017)",            dict(lat=31.7177, lon=-83.2627)),
    ("Taylor - Butler (13269, GMASS consult.)",  dict(lat=32.5571, lon=-84.2380)),
    # residential point: courthouse (-83.9788,30.8366) lands on a 'ROW PARCELS' road right-of-way placeholder
    ("Thomas - Thomasville resid. (13275)",      dict(lat=30.8230, lon=-83.9920)),
    ("Bryan - Pembroke (13029, LASTNAME)",       dict(lat=32.1407, lon=-81.6262)),
    # honest-routing check: web-app-only must NOT resolve
    ("Appling - Baxley (13001, web-app-only)",   dict(lat=31.7785, lon=-82.3485)),
]
print("Georgia sweep smoke test (per-county owner layers)\n" + "=" * 74)
for label, kw in TESTS:
    try:
        r = resolve(**kw)
    except Exception as e:
        print(f"\n{label}\n   ERROR {type(e).__name__}: {e}"); continue
    print(f"\n{label}")
    print(f"   {r.get('county')} {r.get('state')} ({r.get('fips')}) method={r.get('method')} status={r.get('status')}")
    if r.get("status") == "resolved":
        print(f"   OWNER : {r['owner']}" + ("  [~approx]" if r.get("approx") else "") +
              (f"   value: {r['value']}" if r.get("value") else ""))
    else:
        print(f"   note  : {r.get('note','')}")
