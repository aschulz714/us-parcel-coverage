"""Smoke test: Michigan per-county owner sweep (2026-06-08). Focus: wkid-6494 counties where the
distance-buffer returns 0 (Arenac/Barry) must still resolve via resolve.py's EXACT point-first try;
collision guards (Bay vs FL, Jackson), dotted/typo owner fields, self-hosts; + 2 BS&A web-app-only honest."""
import os, sys
sys.path.insert(0, os.path.dirname(__file__))
from resolve import resolve

TESTS = [
    ("Arenac-Standish (26011, wkid6494 exact-only)", dict(lat=43.9836, lon=-83.9588)),
    ("Barry-Hastings (26015, buffer-fail exact-only)",dict(lat=42.6470, lon=-85.2914)),
    ("Bay-Bay City (26017, collision-guard vs FL)",  dict(lat=43.5945, lon=-83.8889)),
    ("Jackson-Jackson (26075, collision MI)",         dict(lat=42.2459, lon=-84.4013)),
    ("Ionia-Ionia (26067, dotted Parcel_Data)",      dict(lat=42.9870, lon=-85.0713)),
    ("Monroe-Monroe (26115, dotted parcelmaster)",   dict(lat=41.9165, lon=-83.3977)),
    ("Mason-Ludington (26105, ParcelMast)",          dict(lat=43.9553, lon=-86.4526)),
    ("Kalamazoo (26077, typo ONERNAME1)",            dict(lat=42.2917, lon=-85.5872)),
    ("Ottawa-Grand Haven (26139, self-host)",        dict(lat=43.0631, lon=-86.2284)),
    ("Allegan (26005, self-host)",                   dict(lat=42.5292, lon=-85.8552)),
    ("Berrien-St Joseph (26021, AGO org)",           dict(lat=42.1097, lon=-86.4803)),
    # honest-routing checks (BS&A Online web-app-only must NOT resolve)
    ("Alcona-Harrisville (26001, BS&A web-app)",     dict(lat=44.6564, lon=-83.2954)),
    ("Alger-Munising (26003, BS&A web-app)",         dict(lat=46.4111, lon=-86.6486)),
]
print("Michigan sweep smoke test (wkid-6494 exact-only + collisions + BS&A web-app)\n" + "=" * 74)
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
print(f"resolved {ok}/{len(TESTS)} (11 arcgis should resolve; Alcona + Alger route honestly)")
