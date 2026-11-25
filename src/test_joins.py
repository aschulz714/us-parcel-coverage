"""Exercise the JOIN-backed + quirky-endpoint counties through resolve.py (the advisor's blind spot)."""
import os, sys
sys.path.insert(0, os.path.dirname(__file__))
from resolve import resolve

TESTS = [
    ("UT Cache / Logan      (JOIN: Owner_Name)",        dict(lat=41.737, lon=-111.835)),
    ("UT Wasatch / Heber    (JOIN: ...OwnerName)",      dict(lat=40.507, lon=-111.413)),
    ("UT Summit / Park City (JOIN: OwnershipTable_OWNER)", dict(lat=40.646, lon=-111.498)),
    ("UT Iron / Cedar City  (JOIN: FABRIC.Tyler_DB.Name)", dict(lat=37.677, lon=-113.061)),
    ("UT Sevier / Richfield (direct, layer 76)",        dict(lat=38.773, lon=-112.085)),
    ("UT Emery / Castle Dale(direct, NAYME)",           dict(lat=39.211, lon=-111.020)),
    ("UT Garfield / Panguitch(direct, 60% pop)",        dict(lat=37.823, lon=-112.436)),
    ("NV Nye / Tonopah      (direct, assess_name)",     dict(lat=38.067, lon=-117.230)),
]
for label, kw in TESTS:
    try:
        r = resolve(**kw)
    except Exception as e:
        print(f"{label}\n   ERROR {type(e).__name__}: {e}\n"); continue
    print(label)
    print(f"   {r.get('county')} {r.get('state')} method={r.get('method')} status={r.get('status')}")
    print(f"   OWNER={r.get('owner')}" + ("  [~buffered]" if r.get("approx") else "") + f"  value={r.get('value','')}\n")
