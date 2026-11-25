"""Smoke test: Alabama per-county owner sweep (2026-06-08), KCS-vendor harvest + self-hosts + AGO + edge cases.
Run: python src/test_al_sweep.py
Covers all KCS host families (web3/4/5/6 + alNNportal), self-hosts (Calhoun/Limestone), AGO orgs (Autauga/Dallas/
Greene), the CaptureCAMA snapshot (Henry — count populated but attribute-sample empty -> confirm point query resolves),
and 2 Flagship web-app-only counties that MUST route honestly."""
import os, sys
sys.path.insert(0, os.path.dirname(__file__))
from resolve import resolve

TESTS = [
    ("Cherokee-Centre (01019, KCS web3)",        dict(lat=34.1526, lon=-85.6786)),
    ("Baldwin-Bay Minette (01003, KCS web6)",    dict(lat=30.8829, lon=-87.7730)),
    ("Blount-Oneonta (01009, KCS web5)",         dict(lat=33.9487, lon=-86.4725)),
    ("Colbert-Tuscumbia (01033, KCS al20portal)",dict(lat=34.7301, lon=-87.7025)),
    ("DeKalb-Fort Payne (01049, KCS al28portal)",dict(lat=34.4443, lon=-85.7197)),
    ("Talladega (01121, KCS al61portal)",        dict(lat=33.4359, lon=-86.1058)),
    ("Russell-Phenix City (01113, KCS web4)",    dict(lat=32.4710, lon=-85.0008)),
    ("Calhoun-Anniston (01015, self-host)",      dict(lat=33.6598, lon=-85.8316)),
    ("Limestone-Athens (01083, self-host)",      dict(lat=34.8020, lon=-86.9716)),
    ("Autauga-Prattville (01001, AGO MigratedOwners)", dict(lat=32.4640, lon=-86.4597)),
    ("Dallas-Selma (01047, AGO)",                dict(lat=32.4074, lon=-87.0211)),
    ("Greene-Eutaw (01063, AGO FULLNAME)",       dict(lat=32.8421, lon=-87.8872)),
    ("Henry-Abbeville (01067, CaptureCAMA 2023)",dict(lat=31.5713, lon=-85.2516)),
    # honest-routing checks: Flagship web-app-only must NOT resolve
    ("Barbour-Eufaula (01005, Flagship web-app)",dict(lat=31.8910, lon=-85.1455)),
    ("Chilton-Clanton (01021, Flagship web-app)",dict(lat=32.8385, lon=-86.6291)),
]
print("Alabama sweep smoke test (KCS harvest + self-hosts + AGO + Flagship)\n" + "=" * 74)
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
print(f"resolved {ok}/{len(TESTS)} (13 arcgis should resolve; the 2 Flagship checks should route honestly)")
