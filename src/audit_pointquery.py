"""Comprehensive point-queryability audit (2026-06-08).
For each UNIQUE resolvable endpoint, fetch a REAL parcel's coordinate (outSR=4326) and round-trip it
through a point query — the definitive test for SR-mismatch / field-error / dead-endpoint bugs that a
count-query census cannot catch (e.g. a layer that is 100% owner-populated but ignores inSR=4326).
Dedupes by endpoint (a statewide layer shared by N counties is tested once). Threaded.
Usage: python src/audit_pointquery.py
"""
import csv, json, ssl, gzip, urllib.request, urllib.parse, concurrent.futures as cf, collections, os
HERE=os.path.dirname(__file__)
CH={'User-Agent':'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36'}
ctx=ssl.create_default_context(); ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE

def getj(url, t=25):
    for c in (None, ctx):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=CH), timeout=t, context=c) as r:
                b=r.read()
                if b[:2]==b'\x1f\x8b': b=gzip.decompress(b)
                return json.loads(b.decode('utf-8','replace'))
        except urllib.error.HTTPError as e: return {'__http__': e.code}
        except Exception as e: last=type(e).__name__
    return {'__err__': last}

def q(base, params): return getj(base.split('/query')[0] + '/query?' + urllib.parse.urlencode(params))

def centroid(geom):
    # mean of the first ring's vertices -> an interior-ish point for typical parcels
    rings = geom.get('rings') or geom.get('paths')
    if not rings:
        # point geometry
        if 'x' in geom: return geom['x'], geom['y']
        return None
    pts = rings[0]
    xs=[p[0] for p in pts]; ys=[p[1] for p in pts]
    return sum(xs)/len(xs), sum(ys)/len(ys)

def first_field(s): return (s or '').replace('|',',').split(',')[0].strip()
def all_fields(s): return [f.strip() for f in (s or '').replace('|',',').split(',') if f.strip()]

def owner_populated(attrs, fields):
    for f in fields:
        v = attrs.get(f) or attrs.get(f.split('.')[-1])
        if v not in (None,'',' ') and not str(v).strip() in ('','0'):
            return str(v)[:40]
    return None

def test_arcgis(qurl, owner_fields):
    own=first_field(owner_fields); fields=all_fields(owner_fields)
    base=qurl
    # 1) get a real parcel point in 4326
    s=q(base, {'where': f"{own} IS NOT NULL", 'outFields': own, 'returnGeometry':'true', 'outSR':4326, 'resultRecordCount':1, 'f':'json'})
    if '__http__' in s or '__err__' in s:
        # owner-filter may fail on dotted/quirk fields -> try where=1=1
        s=q(base, {'where':'1=1','returnGeometry':'true','outSR':4326,'resultRecordCount':1,'f':'json'})
    feats=s.get('features')
    if not feats:
        return ('NO-SAMPLE', s.get('__http__') or s.get('__err__') or str(s)[:30])
    pt=centroid(feats[0].get('geometry') or {})
    if not pt: return ('NO-GEOM','')
    lon,lat=pt
    # 2) point-query at that parcel's location
    p=q(base, {'geometry': f"{lon},{lat}", 'geometryType':'esriGeometryPoint','inSR':4326,'spatialRel':'esriSpatialRelIntersects','distance':150,'units':'esriSRUnit_Meter','outFields':'*','returnGeometry':'false','f':'json'})
    pf=p.get('features')
    if pf is None: return ('POINT-ERR', p.get('__http__') or p.get('__err__') or str(p)[:30])
    if not pf: return ('SR-MISMATCH','point at own parcel returned 0')
    own_val=None
    for ft in pf:
        own_val=owner_populated(ft.get('attributes',{}), fields)
        if own_val: break
    if not own_val: return ('POINT-NO-OWNER','parcel found but owner blank')
    return ('OK', own_val)

def test_join(qurl, jurl, join_key, owner_fields):
    gfield, _, tfield = (join_key or '').partition('='); tfield=tfield or gfield
    fields=all_fields(owner_fields)
    # sample a geom feature in 4326
    s=q(qurl, {'where':'1=1','outFields':gfield,'returnGeometry':'true','outSR':4326,'resultRecordCount':1,'f':'json'})
    feats=s.get('features')
    if not feats: return ('NO-SAMPLE', s.get('__http__') or s.get('__err__') or str(s)[:30])
    pt=centroid(feats[0].get('geometry') or {})
    if not pt: return ('NO-GEOM','')
    lon,lat=pt
    g=q(qurl, {'geometry': f"{lon},{lat}",'geometryType':'esriGeometryPoint','inSR':4326,'spatialRel':'esriSpatialRelIntersects','distance':150,'units':'esriSRUnit_Meter','outFields':gfield,'returnGeometry':'false','f':'json'})
    gf=g.get('features')
    if gf is None: return ('POINT-ERR', g.get('__http__') or g.get('__err__') or '')
    if not gf: return ('SR-MISMATCH','geom point returned 0')
    key=None
    for ft in gf:
        v=ft['attributes'].get(gfield) or ft['attributes'].get(gfield.split('.')[-1])
        if v not in (None,'',' '): key=v; break
    if key is None: return ('NO-KEY','geom feature has blank join key')
    where=f"{tfield}='{key}'" if isinstance(key,str) else f"{tfield}={key}"
    t=q(jurl, {'where':where,'outFields':'*','returnGeometry':'false','f':'json'})
    tf=t.get('features')
    if not tf: return ('JOIN-NO-MATCH', f"{tfield}={key}")
    own_val=owner_populated(tf[0].get('attributes',{}), fields)
    if not own_val: return ('JOIN-NO-OWNER','table row but owner blank')
    return ('OK', own_val)

rows=list(csv.DictReader(open(os.path.join(HERE,'county_methods.csv'))))
def resolvable(r): return r['method'] in ('arcgis','arcgis-join') and (r['method']=='arcgis' or (r.get('join_url') or '').strip())
targets=[r for r in rows if resolvable(r)]
# dedupe by endpoint signature
byep=collections.OrderedDict()
for r in targets:
    sig=(r['method'], r['query_url'], r.get('join_url',''), r['owner_fields'], r.get('join_key',''))
    byep.setdefault(sig, []).append(r)
print(f"Resolvable arcgis/join: {len(targets)} counties on {len(byep)} unique endpoints. Auditing endpoints...")

def run(sig):
    method,qurl,jurl,own,jkey=sig
    try:
        if method=='arcgis-join': return sig, test_join(qurl,jurl,jkey,own)
        return sig, test_arcgis(qurl,own)
    except Exception as e:
        return sig, ('AUDIT-ERR', type(e).__name__)

res={}
with cf.ThreadPoolExecutor(max_workers=20) as ex:
    for sig,out in ex.map(run, list(byep.keys())): res[sig]=out

# tally by status, counting COUNTIES (not endpoints)
status_counties=collections.Counter()
flagged=[]
for sig,(st,detail) in res.items():
    n=len(byep[sig])
    status_counties[st]+=n
    if st!='OK':
        for r in byep[sig]:
            flagged.append((r['fips'],r['state'],r['county'],r['method'],st,detail))
print("\n=== POINT-QUERYABILITY AUDIT (by COUNTY count) ===")
for k,v in status_counties.most_common(): print(f"  {k:16} {v}")
print(f"\n=== FLAGGED COUNTIES: {len(flagged)} (non-OK) ===")
for f,s,c,m,st,d in sorted(flagged, key=lambda x:(x[4],x[1],x[0])):
    print(f"  {f} {s} {c[:20]:20} [{m}] {st}: {str(d)[:40]}")
