"""Smoke test: Wave-1 sweep jackpots end-to-end. Run: python src/test_sweep1.py"""
import os, sys
sys.path.insert(0, os.path.dirname(__file__))
from resolve import resolve

TESTS = [
    ("FL Miami (Miami-Dade)",            dict(lat=25.774, lon=-80.191)),
    ("WI Milwaukee (Milwaukee)",         dict(lat=43.039, lon=-87.906)),
    ("MA Boston (Suffolk)",              dict(lat=42.360, lon=-71.058)),
    ("NC Charlotte (Mecklenburg, /secure/)", dict(lat=35.227, lon=-80.843)),
    ("AR Little Rock (Pulaski, broken-TLS SSL fallback)", dict(lat=34.746, lon=-92.289)),
    ("TN Gallatin (Sumner, statewide-86)", dict(lat=36.388, lon=-86.447)),
    ("TN Nashville (Davidson, own REST)", dict(lat=36.163, lon=-86.781)),
    ("MN Minneapolis (Hennepin, proxy)", dict(lat=44.978, lon=-93.265)),
]
print("Wave-1 sweep smoke test\n" + "=" * 74)
for label, kw in TESTS:
    try:
        r = resolve(**kw)
    except Exception as e:
        print(f"\n{label}\n   ERROR {type(e).__name__}: {e}"); continue
    print(f"\n{label}")
    print(f"   {r.get('county')} {r.get('state')} ({r.get('fips')}) method={r.get('method')}")
    if r.get("status") == "resolved":
        print(f"   OWNER : {r['owner']}" + ("  [~buffered]" if r.get("approx") else "") +
              (f"   value: {r['value']}" if r.get("value") else ""))
    else:
        print(f"   status={r.get('status')}  {r.get('note','')}")
