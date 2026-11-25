"""Smoke test: Pennsylvania per-county owner sweep (2026-06-08). Covers the PASDA per-county MapServer
cluster (Adams/Bedford/Mercer), county self-hosts + AGO orgs, the dotted Lycoming field, the McKean
arcgis-join, Delaware's OWNCAT (alias=OWNER, real names), and 2 honest-routing checks (Blair web-app, Armstrong none)."""
import os, sys
sys.path.insert(0, os.path.dirname(__file__))
from resolve import resolve

TESTS = [
    ("Adams-Gettysburg (42001, PASDA per-county)",  dict(lat=39.8309, lon=-77.2311)),
    ("Bedford-Bedford (42009, PASDA per-county)",   dict(lat=40.0187, lon=-78.5036)),
    ("Mercer-Mercer (42085, PASDA per-county)",     dict(lat=41.2270, lon=-80.2387)),
    ("Beaver-Beaver (42007, county self-host)",     dict(lat=40.6953, lon=-80.3045)),
    ("Delaware-Media (42045, OWNCAT=OWNER alias)",  dict(lat=39.9168, lon=-75.3877)),
    ("Lancaster (42071, county self-host)",         dict(lat=40.0379, lon=-76.3055)),
    ("Lycoming-Williamsport (42081, dotted DBO)",   dict(lat=41.2412, lon=-77.0011)),
    ("McKean-Smethport (42083, arcgis-join)",       dict(lat=41.8098, lon=-78.4439)),
    ("Butler (42019, county self-host)",            dict(lat=40.8612, lon=-79.8953)),
    ("Westmoreland-Greensburg (42129, self-host)",  dict(lat=40.3015, lon=-79.5389)),
    ("Erie (42049, county self-host)",              dict(lat=42.1292, lon=-80.0851)),
    # honest-routing checks
    ("Blair-Altoona (42013, web-app-only)",         dict(lat=40.5187, lon=-78.3947)),
    ("Armstrong-Kittanning (42005, none)",          dict(lat=40.8159, lon=-79.5220)),
]
print("Pennsylvania sweep smoke test (PASDA per-county + self-hosts + join + edge cases)\n" + "=" * 74)
ok = 0
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
        ok += 1
    else:
        print(f"   note  : {r.get('note','')}")
print("\n" + "=" * 74)
print(f"resolved {ok}/{len(TESTS)} (11 arcgis/join should resolve; Blair + Armstrong route honestly)")
