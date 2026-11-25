"""Smoke test: Illinois per-county owner sweep (2026-06-08). Focus: dotted join-view fields that give
empty attribute samples (Coles/Grundy/Knox via colesco/maps.grundyco/knoxcountyil) must resolve on a POINT
query; White (AL-mailing sample) must confirm IL not a collision; plus general coverage + 2 web-app-only honest."""
import os, sys
sys.path.insert(0, os.path.dirname(__file__))
from resolve import resolve

TESTS = [
    # state-collision check (sample showed AL mailing + 'Indian Creek')
    ("White-Carmi (17193, confirm IL not collision)", dict(lat=38.0903, lon=-88.1586)),
    # dotted join-view fields (empty attribute sample -> must resolve on point query)
    ("Coles-Charleston (17029, dotted gis_data.owner1)", dict(lat=39.4961, lon=-88.1761)),
    ("Grundy-Morris (17063, dotted GrundyParcels.dbo)",  dict(lat=41.3575, lon=-88.4226)),
    ("Knox-Galesburg (17095, dotted GIS_Table.dbo)",     dict(lat=40.9478, lon=-90.3712)),
    ("Moultrie-Sullivan (17139, colesco host)",          dict(lat=39.5953, lon=-88.6078)),
    ("Lee-Dixon (17103, dotted dbo_gis_data)",           dict(lat=41.8389, lon=-89.4795)),
    # general coverage spread
    ("Christian-Taylorville (17021, AGO org)",           dict(lat=39.5489, lon=-89.2945)),
    ("DuPage-Wheaton (17043, county server)",            dict(lat=41.8661, lon=-88.1070)),
    ("Hancock-Carthage (17067, gis.wiu.edu cluster)",    dict(lat=40.4169, lon=-91.1404)),
    ("Tazewell-Pekin (17179, county server)",            dict(lat=40.5675, lon=-89.6407)),
    ("St. Clair-Belleville (17163, county server)",      dict(lat=38.5201, lon=-89.9840)),
    # honest-routing checks (web-app-only must NOT resolve)
    ("Champaign-Urbana (17019, CCGISC token-gated)",     dict(lat=40.1106, lon=-88.2073)),
    ("Alexander-Cairo (17003, DevNet web-app)",          dict(lat=37.0053, lon=-89.1764)),
]
print("Illinois sweep smoke test (dotted join-views + collisions + coverage)\n" + "=" * 74)
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
print(f"resolved {ok}/{len(TESTS)} (11 arcgis should resolve; the 2 web-app-only should route honestly)")
