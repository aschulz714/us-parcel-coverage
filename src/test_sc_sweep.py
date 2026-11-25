"""Smoke test: South Carolina per-county owner sweep (2026-06-08). Focus: fully-qualified SDE owner
fields (Beaufort sde.SDE.Gisfile.Owner1, Saluda SDE.DBO.AssessorData.Name), the utility.arcgis.com proxy
host (Aiken), the AECOM host where 'Owner' is a NULL decoy (Calhoun=Name1|Name2), self-hosts; + 2 web-app honest."""
import os, sys
sys.path.insert(0, os.path.dirname(__file__))
from resolve import resolve

TESTS = [
    ("Aiken (45003, utility.arcgis proxy)",      dict(lat=33.5604, lon=-81.7196)),
    ("Beaufort (45013, qualified SDE Owner1)",   dict(lat=32.4316, lon=-80.6698)),
    ("Berkeley-Moncks Corner (45015, Patriot)",  dict(lat=33.1960, lon=-80.0098)),
    ("Calhoun-St Matthews (45017, AECOM Name1)", dict(lat=33.6618, lon=-80.7765)),
    ("Florence (45041, self-host)",              dict(lat=34.1954, lon=-79.7626)),
    ("Horry-Conway (45051, self-host)",          dict(lat=33.8360, lon=-79.0478)),
    ("Oconee-Walhalla (45073, self-host)",       dict(lat=34.7654, lon=-83.0635)),
    ("Saluda (45081, long qualified SDE field)", dict(lat=34.0007, lon=-81.7717)),
    ("Sumter (45085, self-host)",                dict(lat=33.9204, lon=-80.3414)),
    ("Colleton-Walterboro (45029, AGO org)",     dict(lat=32.9050, lon=-80.6668)),
    ("York (45091, AGO org)",                    dict(lat=34.9954, lon=-81.2445)),
    # honest-routing checks
    ("Allendale (45005, Schneider qPublic)",     dict(lat=33.0074, lon=-81.3084)),
    ("Anderson (45007, owner suppressed in REST)",dict(lat=34.5034, lon=-82.6501)),
]
print("South Carolina sweep smoke test (qualified SDE fields + proxy + decoy + self-hosts)\n" + "=" * 74)
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
print(f"resolved {ok}/{len(TESTS)} (11 arcgis should resolve; Allendale + Anderson route honestly)")
