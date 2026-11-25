"""Smoke test: Wave-2 sweep jackpots/partials end-to-end (metro + rural per state).
Run: python src/test_sweep2.py"""
import os, sys
sys.path.insert(0, os.path.dirname(__file__))
from resolve import resolve

TESTS = [
    ("VT metro - Burlington (Chittenden)",        dict(lat=44.4759, lon=-73.2121)),
    ("VT rural - St. Johnsbury (Caledonia)",      dict(lat=44.4192, lon=-72.0151)),
    ("WV metro - Charleston (Kanawha, road-gap->buffer)", dict(lat=38.3498, lon=-81.6326)),
    ("WV rural - Lewisburg (Greenbrier)",         dict(lat=37.8018, lon=-80.4456)),
    ("IA metro - Des Moines (Polk, 2017 vintage)", dict(lat=41.5868, lon=-93.6091)),
    ("IA rural - Decorah (Winneshiek)",           dict(lat=43.3033, lon=-91.7857)),
    ("CT metro - Hartford (Capitol 09110, 6434->envelope)", dict(lat=41.7658, lon=-72.6734)),
    ("CT rural - Torrington (NW Hills 09160)",    dict(lat=41.8007, lon=-73.1212)),
    ("OH metro - Columbus (Franklin)",            dict(lat=39.9612, lon=-82.9988)),
    ("OH rural - Marietta (Washington)",          dict(lat=39.4150, lon=-81.4549)),
    ("OH STRIP - Dayton (Montgomery=unprobed, owner stripped)", dict(lat=39.7589, lon=-84.1916)),
]
print("Wave-2 sweep smoke test (metro + rural)\n" + "=" * 74)
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
