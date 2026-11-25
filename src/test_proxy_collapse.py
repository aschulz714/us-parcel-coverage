"""Smoke test: the 2026-06-08 regional-proxy collapses (VA HRGeo, KY LINK-GIS, MO SEMO RPC).
Run: python src/test_proxy_collapse.py
Covers the 3 verified regional hosts + the two MO edge cases (StFrancois/Washington join-view owner
that materializes only on a point query; Iron's dotted Parcel.DEEDNAME field)."""
import os, sys
sys.path.insert(0, os.path.dirname(__file__))
from resolve import resolve

TESTS = [
    # VA — HRGeo (one layer, OWNERNME1, 11 of 16 Hampton Roads jurisdictions)
    ("VA Virginia Beach (51810, HRGeo)",     dict(lat=36.8410, lon=-76.1380)),
    ("VA Norfolk (51710, HRGeo)",            dict(lat=36.8466, lon=-76.2917)),
    ("VA Newport News (51700, HRGeo)",       dict(lat=37.0871, lon=-76.4730)),
    ("VA Williamsburg city (51830, HRGeo)",  dict(lat=37.2707, lon=-76.7075)),
    # KY — LINK-GIS (Parcel_QueryOnly layers 0/2)
    ("KY Campbell-Newport (21037, OWNER_NAME_1)", dict(lat=39.0917, lon=-84.4958)),
    ("KY Pendleton-Falmouth (21191, Name)",       dict(lat=38.6759, lon=-84.3327)),
    # MO — SEMO RPC (maps.semogis.com); join-view + dotted-field edge cases
    ("MO St.Francois-Farmington (29187, join-view OWNER_NAME)", dict(lat=37.7806, lon=-90.4218)),
    ("MO Washington-Potosi (29221, join-view NAME1)",           dict(lat=37.9356, lon=-90.7879)),
    ("MO Iron-Ironton (29093, dotted Parcel.DEEDNAME)",         dict(lat=37.5970, lon=-90.6248)),
    ("MO Cape Girardeau (29031, Main_Name)",                    dict(lat=37.3059, lon=-89.5181)),
    ("MO Perry-Perryville (29157, Deedholder)",                 dict(lat=37.7242, lon=-89.8612)),
    ("MO Scott-Sikeston (29201, NAME)",                         dict(lat=36.8767, lon=-89.5879)),
    ("MO Madison-Fredericktown (29123, Owner1)",                dict(lat=37.5589, lon=-90.2926)),
]
print("Regional-proxy collapse smoke test (VA HRGeo / KY LINK-GIS / MO SEMO RPC)\n" + "=" * 74)
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
print(f"resolved {ok}/{len(TESTS)} (all 13 should resolve a real owner)")
