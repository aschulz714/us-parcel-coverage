"""Owner-quality census for the 72 arcgis-join + 77 wfs counties the main audit skipped (~6%).
Resolver-grounded, per county: run resolve() at the county interior point; classify populated /
blank / no-parcel. The non-clean ones get a county-SCOPED sample fallback (interior points miss
parcels in rural counties -- the same lesson from the arcgis pass). Truth = the live resolver.
Usage: python src/audit_join_wfs.py
"""
import csv, json, ssl, gzip, urllib.request, urllib.parse, concurrent.futures as cf, collections, os, sys
sys.path.insert(0, os.path.dirname(__file__))
from resolve import resolve

HERE = os.path.dirname(__file__)
CH = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36'}
ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE

def getj(url, t=30):
    for c in (None, ctx):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=CH), timeout=t, context=c) as r:
                b = r.read()
                if b[:2] == b'\x1f\x8b': b = gzip.decompress(b)
                return json.loads(b.decode('utf-8', 'replace'))
        except urllib.error.HTTPError as e: return {'__http__': e.code}
        except Exception as e: last = type(e).__name__
    return {'__err__': last}

rows = {r['fips']: r for r in csv.DictReader(open(os.path.join(HERE, 'county_methods.csv'), encoding='utf-8'))}
targets = [r for r in rows.values() if (r['method'] == 'arcgis-join' and (r.get('join_url') or '').strip()) or r['method'] == 'wfs']
geo = json.load(open(os.path.join(HERE, '..', 'data', 'us_counties.geojson')))

def interior(g):
    c = g['coordinates']; ringset = c if g['type'] == 'Polygon' else max(c, key=lambda p: len(p[0]))
    ring = ringset[0]; xs = [p[0] for p in ring]; ys = [p[1] for p in ring]
    return sum(xs) / len(xs), sum(ys) / len(ys)
pts = {f['properties']['STATE'] + f['properties']['COUNTY']: interior(f['geometry']) for f in geo['features']}

# county-scoped sample fallback: get one real parcel point inside the county from its own layer
def scoped_point(r):
    m = r['method']; fp = r['fips']
    if m == 'wfs':
        # OK: CQL on county name
        cty = r['county'].replace(' County', '').replace(' Parish', '').upper()
        base = r['query_url'].split('?')[0]
        url = base + '?' + urllib.parse.urlencode({'service': 'WFS', 'version': '2.0.0', 'request': 'GetFeature',
              'typeNames': 'current_statewide_parcels', 'outputFormat': 'application/json', 'count': 1,
              'srsName': 'EPSG:4326', 'CQL_FILTER': f"county='{cty}' AND owner IS NOT NULL"})
        d = getj(url); feats = d.get('features') if isinstance(d, dict) else None
        if not feats: return None
        g = feats[0].get('geometry') or {}; co = g.get('coordinates')
        if not co: return None
        # polygon -> first ring centroid
        ring = co[0][0] if g.get('type') == 'MultiPolygon' else co[0]
        return sum(p[0] for p in ring) / len(ring), sum(p[1] for p in ring) / len(ring)
    # arcgis-join: sample geom layer, filter by county where possible
    base = r['query_url']
    gfield = (r['join_key'] or '').partition('=')[0]
    where = '1=1'
    if r['state'] == 'ND': where = f"CountyName='{r['county'].replace(' County','')}'"
    elif r['state'] == 'ME': where = f"COUNTY='{r['county'].replace(' County','').upper()}'"
    d = getj(base + '?' + urllib.parse.urlencode({'where': where, 'outFields': gfield, 'returnGeometry': 'true',
             'outSR': 4326, 'resultRecordCount': 1, 'f': 'json'}))
    feats = d.get('features') if isinstance(d, dict) else None
    if not feats:
        d = getj(base + '?' + urllib.parse.urlencode({'where': '1=1', 'outFields': gfield, 'returnGeometry': 'true',
                 'outSR': 4326, 'resultRecordCount': 1, 'f': 'json'}))
        feats = d.get('features') if isinstance(d, dict) else None
    if not feats: return None
    g = feats[0].get('geometry') or {}; rings = g.get('rings')
    if not rings: return None
    ring = max(rings, key=len)
    return sum(p[0] for p in ring) / len(ring), sum(p[1] for p in ring) / len(ring)

def check(r):
    fp = r['fips']
    # 1) interior-point resolve
    p = pts.get(fp)
    res = None
    if p:
        try: res = resolve(lat=p[1], lon=p[0])
        except Exception as e: res = {'status': 'EXC', 'owner': None, 'note': type(e).__name__}
    ok = res and res.get('status') == 'resolved' and res.get('owner')
    if ok:
        return fp, ('POPULATED', res.get('owner'), res.get('fips') == fp)
    # 2) county-scoped sample fallback
    try:
        sp = scoped_point(r)
    except Exception:
        sp = None
    if sp:
        try: res2 = resolve(lat=sp[1], lon=sp[0])
        except Exception as e: res2 = {'status': 'EXC', 'owner': None}
        if res2 and res2.get('status') == 'resolved' and res2.get('owner'):
            return fp, ('POPULATED-SCOPED', res2.get('owner'), res2.get('fips') == fp)
        return fp, ('BLANK-OR-NOPARCEL', (res2 or {}).get('status'), (res or {}).get('status'))
    return fp, ('NO-SAMPLE', (res or {}).get('status'), None)

print(f"Censusing {len(targets)} join/wfs counties (resolver-grounded)...")
out = {}
with cf.ThreadPoolExecutor(max_workers=12) as ex:
    futs = {ex.submit(check, r): r for r in targets}
    for fu in cf.as_completed(futs):
        fp, v = fu.result(); out[fp] = v

by = collections.Counter(v[0] for v in out.values())
print("\n=== JOIN/WFS OWNER CENSUS (by county) ===")
for k, n in by.most_common(): print(f"  {k:20} {n}")
# group POPULATED by state
pop_by_state = collections.Counter(rows[fp]['state'] for fp, v in out.items() if v[0].startswith('POPULATED'))
print("\n  POPULATED by state:", dict(sorted(pop_by_state.items())))
print("\n  NON-CLEAN (needs eyeball):")
for fp, v in sorted(out.items()):
    if not v[0].startswith('POPULATED'):
        print(f"  {fp} {rows[fp]['state']} {rows[fp]['county'][:20]:20} {v[0]}: resolve={v[1]} interior={v[2]}")
print("\n  SAMPLE owners (first 12 populated):")
for fp, v in list((fp, v) for fp, v in sorted(out.items()) if v[0].startswith('POPULATED'))[:12]:
    print(f"  {fp} {rows[fp]['state']} {rows[fp]['county'][:18]:18} -> {str(v[1])[:45]}")
json.dump(out, open(os.path.join(HERE, '..', 'data', 'join_wfs_census.json'), 'w'), indent=1)
