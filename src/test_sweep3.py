"""Smoke test: Wave-3 sweep jackpots/partials end-to-end (metro + rural per state).
Covers the two NEW resolver methods: wfs (OK) and arcgis-join (ND). Run: python src/test_sweep3.py"""
import os, sys
sys.path.insert(0, os.path.dirname(__file__))
from resolve import resolve

TESTS = [
    ("TX metro - Houston (Harris)",                  dict(lat=29.7604, lon=-95.3698)),
    ("TX rural - Alpine (Brewster, far west)",       dict(lat=30.3585, lon=-103.6610)),
    ("MS West/1 - Jackson (Hinds)",                  dict(lat=32.2988, lon=-90.1848)),
    ("MS East/2 - Meridian (Lauderdale)",            dict(lat=32.3643, lon=-88.7034)),
    ("NY metro - Albany (36001, opt-in)",            dict(lat=42.6526, lon=-73.7562)),
    ("NY rural - Cooperstown (Otsego 36077)",        dict(lat=42.7000, lon=-74.9246)),
    ("NY uncov - Rochester (Monroe 36055, not opt-in -> unprobed)", dict(lat=43.1566, lon=-77.6088)),
    ("OK metro - Oklahoma City (WFS, GeoServer)",    dict(lat=35.4676, lon=-97.5164)),
    ("OK rural - Guymon (Texas Co, panhandle)",      dict(lat=36.6828, lon=-101.4818)),
    ("ND metro - Bismarck (Burleigh, arcgis-join)",  dict(lat=46.8083, lon=-100.7837)),
    ("ND rural - Williston (Williams, Bakken)",      dict(lat=48.1470, lon=-103.6180)),
]
print("Wave-3 sweep smoke test (metro + rural; wfs + arcgis-join methods)\n" + "=" * 74)
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
    else:
        print(f"   note  : {r.get('note','')}")
