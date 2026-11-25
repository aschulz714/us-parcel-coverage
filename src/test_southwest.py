"""Smoke test: resolve real Southwest facilities end-to-end. Run: python src/test_southwest.py"""
import os, sys
sys.path.insert(0, os.path.dirname(__file__))
from resolve import resolve

TESTS = [
    ("AZ Phoenix downtown (Maricopa, own server)", dict(lat=33.448, lon=-112.074)),
    ("AZ Florence (Pinal, via AZ-DWR / Cloudflare WAF)", dict(lat=33.038, lon=-111.387)),
    ("AZ Safford (Graham, via AZ-DWR / WAF)", dict(lat=32.834, lon=-109.708)),
    ("AZ Tucson (Pima, owner in MAIL1)", dict(lat=32.222, lon=-110.974)),
    ("AZ Flagstaff (Coconino = web-app-only)", dict(lat=35.198, lon=-111.651)),
    ("NM Albuquerque (Bernalillo, sibling layer 0)", dict(lat=35.084, lon=-106.650)),
    ("NM Santa Fe (Santa Fe, sibling layer 26)", dict(lat=35.687, lon=-105.937)),
    ("NM Las Cruces (Doña Ana, sibling layer 7)", dict(lat=32.312, lon=-106.778)),
    ("CO Denver (statewide layer)", dict(lat=39.739, lon=-104.990)),
    ("CO Colorado Springs (El Paso, per-county)", dict(lat=38.834, lon=-104.821)),
    ("CO Breckenridge (Summit, per-county)", dict(lat=39.481, lon=-106.038)),
    ("CO Alamosa (unprobed -> honest route)", dict(lat=37.468, lon=-105.870)),
]
print("Southwest owner-resolution smoke test\n" + "=" * 74)
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
