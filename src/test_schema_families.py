"""Schema-family regression: resolve one real point per DISTINCT crosswalk schema family.
Run: python src/test_schema_families.py  — confirms the shared resolver handles every schema
(statewide string/numeric value, opt-in proxy, arcgis-join, dotted SDE join-view, native-SR ESRI+EPSG
reprojection, Integrity Geocortex x-proxy, WinGAP/GARC, PR snapshot). Guards shared-code changes."""
import os,sys; sys.path.insert(0,os.path.dirname(os.path.abspath(__file__)))
from resolve import resolve
FAM=[
 ("statewide string-value (TX StratMap)","48453",30.2672,-97.7431),
 ("statewide services9 (FL)","12095",28.5383,-81.3792),
 ("statewide WI parcels","55025",43.0731,-89.4012),
 ("MN opt-in proxy (owner_name)","27053",44.9778,-93.2650),
 ("arcgis-join (ME MEGIS STATE_ID->OWNER1)","23005",43.6591,-70.2568),
 ("arcgis-join (ND TaxRoll)","38017",46.8772,-96.7898),
 ("dotted SDE join-view (SC Beaufort)","45013",32.4316,-80.6698),
 ("native-SR ESRI:102698 (MO Dade)","29057",37.4119,-93.8438),
 ("native-SR EPSG:2893 (Anne Arundel MD)","24003",39.1620,-76.6100),
 ("Integrity Geocortex x-proxy (MO Bates)","29013",38.2589,-94.3319),
 ("WinGAP lastname (GA Clay)","13061",31.7770,-85.1116),
 ("GARC org Owner_name (GA Chattahoochee)","13053",32.3074,-84.7799),
 ("PR 2018 snapshot 'contact' (San Juan)","72127",18.4655,-66.1057),
 ("statewide TN parcels","47037",36.1627,-86.7816),
]
ok=0
for fam,fips,lat,lon in FAM:
    try:
        r=resolve(lat=lat,lon=lon)
        good=r.get('status')=='resolved' and r.get('owner') and r.get('fips')==fips
        ok+=bool(good)
        print(f"  {'OK ' if good else 'XX '} {fam:42} {r.get('fips')} {str(r.get('owner'))[:30]}")
    except Exception as e: print(f"  ERR {fam:42} {type(e).__name__}:{str(e)[:30]}")
print(f"\n  {ok}/{len(FAM)} schema families OK")
