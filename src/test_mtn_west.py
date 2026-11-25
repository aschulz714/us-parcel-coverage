"""Smoke test: resolve real Mountain West facilities end-to-end (location -> owner).
Hits Census geocoder + live county/statewide ArcGIS layers. Run: python src/test_mtn_west.py"""
import os, sys
sys.path.insert(0, os.path.dirname(__file__))
from resolve import resolve

TESTS = [
    # MT (statewide Cadastral jackpot)
    ("MT  First Interstate Center, Billings (Yellowstone)", dict(address="401 N 31st St, Billings, MT 59101")),
    ("MT  downtown Missoula (Missoula)",                    dict(address="140 N Higgins Ave, Missoula, MT 59802")),
    # WY (statewide DOR jackpot)
    ("WY  Frontier Mall, Cheyenne (Laramie)",               dict(address="1400 Dell Range Blvd, Cheyenne, WY 82009")),
    ("WY  downtown Casper (Natrona)",                       dict(address="234 E 1st St, Casper, WY 82601")),
    # NV arcgis (Washoe) vs web-app-only (Clark) vs web-app-only (Storey/Tesla)
    ("NV  downtown Reno (Washoe = arcgis)",                 dict(address="1 E 1st St, Reno, NV 89501")),
    ("NV  Las Vegas Sands HQ (Clark = web-app-only)",       dict(address="3355 Las Vegas Blvd S, Las Vegas, NV 89109")),
    ("NV  Tesla Gigafactory (Storey = web-app-only)",       dict(lat=39.538, lon=-119.440)),
    # UT arcgis (Salt Lake, Utah Co) vs web-app-only (Davis)
    ("UT  Zions Bancorp HQ, Salt Lake City (Salt Lake)",    dict(address="1 S Main St, Salt Lake City, UT 84133")),
    ("UT  Provo City Center (Utah Co = arcgis)",            dict(address="351 W Center St, Provo, UT 84601")),
    ("UT  Farmington (Davis = web-app-only)",               dict(address="28 E State St, Farmington, UT 84025")),
]

print("Mountain West owner-resolution smoke test\n" + "=" * 74)
for label, kw in TESTS:
    try:
        r = resolve(**kw)
    except Exception as e:
        print(f"\n{label}\n   ERROR: {type(e).__name__}: {e}")
        continue
    print(f"\n{label}")
    print(f"   county={r.get('county')} {r.get('state')} ({r.get('fips')})  method={r.get('method')}")
    if r.get("status") == "resolved":
        print(f"   OWNER : {r['owner']}" + ("  [~buffered]" if r.get("approx") else ""))
        if r.get("value"):
            print(f"   value : {r['value']}")
    else:
        print(f"   status={r.get('status')}  {r.get('note','')}")
