"""Smoke test: Wave-4 sweep closeout (AK/HI/DC), metro+rural where applicable.
Run: python src/test_sweep4.py"""
import os, sys
sys.path.insert(0, os.path.dirname(__file__))
from resolve import resolve

TESTS = [
    ("DC - Washington (11001)",                      dict(lat=38.9072, lon=-77.0369)),
    ("AK metro - Anchorage (02020)",                 dict(lat=61.2181, lon=-149.9003)),
    ("AK rural - Fairbanks (FNSB 02090)",            dict(lat=64.8378, lon=-147.7164)),
    ("AK uncov - Bethel (census area -> unprobed)",  dict(lat=60.7922, lon=-161.7558)),
    ("HI Kauai - Lihue (15007, only owner-bearing island)", dict(lat=21.9811, lon=-159.3711)),
    ("HI Honolulu (15003 -> web-app-only)",          dict(lat=21.3069, lon=-157.8583)),
]
print("Wave-4 sweep smoke test (AK/HI/DC closeout)\n" + "=" * 74)
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
