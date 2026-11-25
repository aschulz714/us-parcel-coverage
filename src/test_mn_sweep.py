"""Smoke test: Minnesota — proxy block (Phase 1) + per-county tail (Phase 2), 2026-06-07.
Run: python src/test_mn_sweep.py
Covers: the statewide opt-in proxy metro (Hennepin), the dotted/qualified join-view owner
fields (Blue Earth/Clearwater/Norman), the TAX_NAME-as-owner proxies (Carver/Scott), a
former proxy-withholder now resolved via its own county server (Stearns), and two web-app-only
counties that MUST route honestly (status != resolved) rather than claim a false owner."""
import os, sys
sys.path.insert(0, os.path.dirname(__file__))
from resolve import resolve

TESTS = [
    ("Hennepin - Minneapolis (27053, proxy)",    dict(lat=44.9778, lon=-93.2650)),
    ("Blue Earth - Mankato (27013, join-view)",  dict(lat=44.1636, lon=-93.9994)),
    ("Clearwater - Bagley (27029, Manatron join)", dict(lat=47.5219, lon=-95.4011)),
    ("Norman - Ada (27107, join-view)",          dict(lat=47.2997, lon=-96.5150)),
    ("Carver - Chaska (27019, TAX_NAME proxy)",  dict(lat=44.7894, lon=-93.6022)),
    ("Scott - Shakopee (27139, TAX_NAME)",       dict(lat=44.7980, lon=-93.5269)),
    ("Stearns - St. Cloud (27145, own server)",  dict(lat=45.5579, lon=-94.1632)),
    ("Beltrami - Bemidji (27007)",               dict(lat=47.4736, lon=-94.8803)),
    # honest-routing check: web-app-only must NOT resolve
    ("McLeod - Glencoe (27085, web-app-only)",   dict(lat=44.7689, lon=-94.1517)),
    ("Pine - Pine City (27115, web-app-only)",   dict(lat=45.8266, lon=-92.9685)),
]
print("Minnesota sweep smoke test (proxy block + per-county tail)\n" + "=" * 74)
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
