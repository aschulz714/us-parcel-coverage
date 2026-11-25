"""Smoke test: South Dakota per-county owner sweep (2026-06-07), metro + rural + edge cases.
Run: python src/test_sd_sweep.py
Covers the dominant SD hosts (District III gis.districtiii.org, First District 1stdistrict.org,
ISG/AGO orgs, county self-hosts), the two VERIFICATION-PASS CORRECTIONS (Hamlin/Hand recovered
from web-app-only -> arcgis), a reservation county where the resolver must blank-skip tribal-trust
parcels to find a real owner (Jackson), and THREE honest-routing checks (Brown/Aberdeen, Brookings,
and the DOWNGRADED Lincoln/Canton) that MUST route honestly (status != resolved), not fake an owner."""
import os, sys
sys.path.insert(0, os.path.dirname(__file__))
from resolve import resolve

TESTS = [
    # --- metro / larger towns (arcgis wins) ---
    ("Codington - Watertown (46029, 1stDistrict OWNER1)", dict(lat=44.8997, lon=-97.1147)),
    ("Hughes - Pierre (46065, ISG OWNER_NAME)",           dict(lat=44.3683, lon=-100.3510)),
    ("Yankton - Yankton (46135, AGO org Name)",           dict(lat=42.8711, lon=-97.3970)),
    ("Davison - Mitchell (46035, DistrictIII OWNERNAME)", dict(lat=43.7094, lon=-98.0298)),
    # --- the two verification-pass corrections (recovered from web-app-only) ---
    ("Hamlin - Estelline (46057, CORRECTED OWNER1)",      dict(lat=44.5786, lon=-96.9034)),
    ("Hand - Miller (46059, CORRECTED OWNER_NAME)",       dict(lat=44.5208, lon=-98.9884)),
    # --- rural, varied owner-field schemas ---
    ("Brule - Chamberlain (46015, TC_OWNER schema)",      dict(lat=43.8108, lon=-99.3309)),
    ("Haakon - Philip (46055, DEEDHOLDER, rural)",        dict(lat=44.0394, lon=-101.6646)),
    ("Stanley - Fort Pierre (46117, CAMA_OWNERNAME)",     dict(lat=44.3547, lon=-100.3735)),
    ("Lawrence - Spearfish (46081, AGO CURRENT_OW)",      dict(lat=44.4908, lon=-103.8590)),
    ("Charles Mix - Lake Andes (46023, deedhold/TC mix)", dict(lat=43.1545, lon=-98.5470)),
    # --- reservation county: resolver must blank-skip trust parcels to reach a real owner ---
    ("Jackson - Kadoka (46071, DEEDHOLDER 43.7% resvn)",  dict(lat=43.8344, lon=-101.5085)),
    # --- honest-routing checks: web-app-only MUST NOT resolve an owner ---
    ("Brown - Aberdeen (46013, web-app-only)",            dict(lat=45.4647, lon=-98.4865)),
    ("Brookings - Brookings (46011, web-app-only)",       dict(lat=44.3114, lon=-96.7984)),
    ("Lincoln - Canton (46083, DOWNGRADED web-app-only)", dict(lat=43.3003, lon=-96.5928)),
]
print("South Dakota sweep smoke test (per-county owner layers)\n" + "=" * 74)
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
print(f"resolved {ok}/{len(TESTS)} (the 3 web-app-only checks SHOULD be unresolved -> ~12 expected)")
