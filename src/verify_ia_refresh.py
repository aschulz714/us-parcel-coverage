"""Verify each REFRESHED Iowa probe is genuinely point-queryable with a current owner.
For each new probe: fetch a real parcel point (outSR=4326) from the recorded layer, round-trip it through a
point query, confirm a real owner returns. A probe that fails here would OVERRIDE 2017 and break the county -> must be reverted."""
import csv, json, ssl, gzip, urllib.request, urllib.parse, os, glob
HERE=os.path.dirname(__file__); ROOT=os.path.join(HERE,'..')
CH={'User-Agent':'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36'}
ctx=ssl.create_default_context(); ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE
def getj(url,t=30):
    for c in (None,ctx):
        try:
            with urllib.request.urlopen(urllib.request.Request(url,headers=CH),timeout=t,context=c) as r:
                b=r.read()
                if b[:2]==b'\x1f\x8b': b=gzip.decompress(b)
                return json.loads(b.decode('utf-8','replace'))
        except urllib.error.HTTPError as e: return {'__http__':e.code}
        except Exception as e: last=type(e).__name__
    return {'__err__':last}
def q(base,p): return getj(base.split('/query')[0]+'/query?'+urllib.parse.urlencode(p))
def centroid(g):
    rings=g.get('rings')
    if not rings: return None
    pts=rings[0]; return sum(p[0] for p in pts)/len(pts), sum(p[1] for p in pts)/len(pts)

# the 25 refreshed IA probes are the IA fips in probes_west
ia=sorted(glob.glob(os.path.join(ROOT,'data','probes_west','19*.json')))
print(f"Verifying {len(ia)} refreshed IA probes...\n")
fails=[]
for pf in ia:
    d=json.load(open(pf)); fips=d['fips']; cty=d['county']
    base=d['query_url']; own=(d['owner_fields'] or '').replace('|',',').split(',')[0].strip()
    fieldslist=[f.strip() for f in (d['owner_fields'] or '').replace('|',',').split(',') if f.strip()]
    # sample a parcel point in 4326
    s=q(base,{'where':f"{own} IS NOT NULL",'outFields':own,'returnGeometry':'true','outSR':4326,'resultRecordCount':1,'f':'json'})
    if not s.get('features'):
        s=q(base,{'where':'1=1','returnGeometry':'true','outSR':4326,'resultRecordCount':1,'f':'json'})
    feats=s.get('features')
    if not feats:
        fails.append((fips,cty,'NO-SAMPLE',s.get('__http__') or s.get('__err__') or str(s)[:40])); continue
    pt=centroid(feats[0].get('geometry') or {})
    if not pt:
        fails.append((fips,cty,'NO-GEOM','sample has no geometry')); continue
    lon,lat=pt
    p=q(base,{'geometry':f'{lon},{lat}','geometryType':'esriGeometryPoint','inSR':4326,'spatialRel':'esriSpatialRelIntersects','distance':120,'units':'esriSRUnit_Meter','outFields':'*','returnGeometry':'false','f':'json'})
    pfe=p.get('features')
    if pfe is None:
        fails.append((fips,cty,'POINT-ERR',p.get('__http__') or p.get('__err__') or str(p)[:40])); continue
    if not pfe:
        fails.append((fips,cty,'SR-MISMATCH','point at own parcel returned 0')); continue
    val=None
    for ft in pfe:
        for f in fieldslist:
            v=ft['attributes'].get(f) or ft['attributes'].get(f.split('.')[-1])
            if v not in (None,'',' ') and str(v).strip() not in ('','0'): val=str(v)[:34]; break
        if val: break
    if not val:
        fails.append((fips,cty,'POINT-NO-OWNER','parcel found, owner blank')); continue
    print(f"  OK  {fips} {cty[:14]:14} v{d.get('note','')[:4]} -> {val!r}")
print(f"\n=== {len(ia)-len(fails)}/{len(ia)} refreshed IA probes VERIFY point-queryable ===")
if fails:
    print(f"=== {len(fails)} FAIL (must REVERT to 2017): ===")
    for fips,cty,why,d in fails: print(f"  !! {fips} {cty[:16]:16} {why}: {d}")
    print("REVERT_FIPS="+",".join(f for f,_,_,_ in fails))
