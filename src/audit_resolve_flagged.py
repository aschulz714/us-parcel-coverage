"""Run the ACTUAL resolver at each flagged county's interior point to separate audit-method
artifacts from genuine bugs. resolver-resolves -> false alarm; resolver-fails -> real."""
import sys, os, json, re
HERE=os.path.dirname(__file__)
sys.path.insert(0, HERE)
from resolve import resolve

# flagged fips from the point-queryability audit. Pass the audit output file as arg 1
# (a text dump with lines like "29185 MO St. Clair [arcgis] SR-MISMATCH: ..."). One-off helper.
AUDIT = sys.argv[1] if len(sys.argv) > 1 else ""
if not AUDIT or not os.path.exists(AUDIT):
    sys.exit("usage: python audit_resolve_flagged.py <point-queryability-audit-output.txt>")
flagged={}
for line in open(AUDIT, encoding='utf-8'):
    m=re.match(r'\s*(\d{5})\s+(\w\w)\s+(.+?)\s+\[(arcgis|arcgis-join)\]\s+([A-Z-]+):', line)
    if m: flagged[m.group(1)]=(m.group(2), m.group(3).strip(), m.group(5))

geo=json.load(open(os.path.join(HERE,'..','data','us_counties.geojson')))
def interior_pt(props_geom):
    # average vertices of the largest ring -> rough interior point (lon,lat) in 4326
    coords=props_geom['coordinates']
    if props_geom['type']=='Polygon': rings=coords
    else:  # MultiPolygon -> pick largest polygon's outer ring
        rings=max(coords, key=lambda poly: len(poly[0]))
    ring=rings[0]
    xs=[p[0] for p in ring]; ys=[p[1] for p in ring]
    return sum(xs)/len(xs), sum(ys)/len(ys)

pts={}
for f in geo['features']:
    fp=f['properties']['STATE']+f['properties']['COUNTY']
    if fp in flagged:
        try: pts[fp]=interior_pt(f['geometry'])
        except Exception as e: pts[fp]=None

print(f"Re-resolving {len(flagged)} flagged counties at interior points...\n")
real=[]
for fp in sorted(flagged):
    st,cty,why=flagged[fp]
    p=pts.get(fp)
    if not p: print(f"  {fp} {st} {cty[:18]:18}: NO-CENTROID"); real.append((fp,st,cty,why,'no-centroid')); continue
    lon,lat=p
    try:
        r=resolve(lat=lat, lon=lon); status=r.get('status'); owner=r.get('owner')
    except Exception as e:
        status='ERR:'+type(e).__name__; owner=None
    ok = status=='resolved' and owner
    if not ok:
        real.append((fp,st,cty,why,f'{status}'))
    # print only non-OK to keep it short
    if not ok:
        print(f"  !! {fp} {st} {cty[:18]:18} (audit:{why}) -> {status} owner={owner}")
print(f"\n=== {len(flagged)-len(real)}/{len(flagged)} flagged counties RESOLVE at centroid (false alarms). ===")
print(f"=== {len(real)} did NOT resolve at centroid (genuine OR centroid-in-rural-gap): ===")
import collections
byst=collections.Counter(r[1] for r in real)
print("  by state:", dict(byst.most_common()))
