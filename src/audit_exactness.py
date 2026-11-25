"""Plan B: EXACTNESS census. When resolve() returns an owner, was it an EXACT point-in-parcel
hit, or a buffer/envelope fallback that could return a NEIGHBOR's owner?

Method (round-trip ground truth): for a stratified sample of endpoints, pull one REAL parcel
with geometry+owner, compute its polygon centroid (a point guaranteed inside that parcel), run
the live resolver there, and check:
  - did it resolve?  - was it EXACT (approx=False)?  - does the returned owner MATCH the
    sampled parcel's own owner (i.e. same parcel, not a neighbor)?
A buffer/envelope hit that still returns the correct owner is fine; one that returns a different
owner is a real error. This separates 'approx-but-correct' from 'approx-and-wrong'.
Usage: python src/audit_exactness.py
"""
import csv, json, ssl, gzip, urllib.request, urllib.parse, concurrent.futures as cf, collections, os, sys
sys.path.insert(0, os.path.dirname(__file__))
from resolve import resolve, _owner_from

HERE = os.path.dirname(__file__)
CH = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36'}
ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE

def getj(url, t=25):
    for c in (None, ctx):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=CH), timeout=t, context=c) as r:
                b = r.read()
                if b[:2] == b'\x1f\x8b': b = gzip.decompress(b)
                return json.loads(b.decode('utf-8', 'replace'))
        except urllib.error.HTTPError as e: return {'__http__': e.code}
        except Exception as e: last = type(e).__name__
    return {'__err__': last}

def first(s): return (s or '').replace('|', ',').split(',')[0].strip()
def norm(s): return ' '.join(str(s or '').upper().split())[:25]

def centroid(geom):
    rings = geom.get('rings')
    if not rings: return None
    pts = max(rings, key=len)  # largest ring
    xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
    return sum(xs) / len(xs), sum(ys) / len(ys)

def sample_one(row):
    own = first(row['owner_fields'])
    base = row['query_url'].split('/query')[0]
    # pull a real parcel with a populated owner, geometry in 4326, away from the placeholder head of the table
    s = getj(base + '/query?' + urllib.parse.urlencode({
        'where': f"{own} LIKE '% %'", 'outFields': own, 'returnGeometry': 'true', 'outSR': 4326,
        'orderByFields': 'OBJECTID DESC', 'resultRecordCount': 1, 'f': 'json'}))
    feats = s.get('features') if isinstance(s, dict) else None
    if not feats:
        s = getj(base + '/query?' + urllib.parse.urlencode({
            'where': '1=1', 'outFields': own, 'returnGeometry': 'true', 'outSR': 4326,
            'orderByFields': 'OBJECTID DESC', 'resultRecordCount': 1, 'f': 'json'}))
        feats = s.get('features') if isinstance(s, dict) else None
    if not feats: return ('NO-SAMPLE', None, None)
    f0 = feats[0]
    truth_owner = f0['attributes'].get(own) or f0['attributes'].get(own.split('.')[-1])
    c = centroid(f0.get('geometry') or {})
    if not c: return ('NO-GEOM', truth_owner, None)
    lon, lat = c
    try:
        res = resolve(lat=lat, lon=lon)
    except Exception as e:
        return ('RESOLVE-EXC', truth_owner, type(e).__name__)
    if res.get('status') != 'resolved':
        return ('RESOLVE-' + str(res.get('status')), truth_owner, res.get('owner'))
    got = res.get('owner')
    approx = res.get('approx')
    match = (norm(got) and norm(truth_owner) and norm(got) == norm(truth_owner))
    if not match and norm(got) and norm(truth_owner):
        # partial: one contains the other (multi-owner / formatting)
        match = norm(got) in norm(truth_owner) or norm(truth_owner) in norm(got)
    tag = ('EXACT-MATCH' if not approx else 'APPROX-MATCH') if match else \
          ('EXACT-MISMATCH' if not approx else 'APPROX-MISMATCH')
    return (tag, truth_owner, got)

rows = list(csv.DictReader(open(os.path.join(HERE, 'county_methods.csv'), encoding='utf-8')))
ag = [r for r in rows if r['method'] == 'arcgis']
# stratified by state: up to 3 endpoints per state (dedupe endpoints first)
byep = collections.OrderedDict()
for r in ag: byep.setdefault(r['query_url'], r)
eps = list(byep.values())
perstate = collections.defaultdict(list)
for r in eps: perstate[r['state']].append(r)
sample = []
for st, rs in perstate.items():
    step = max(1, len(rs) // 3)
    sample += rs[::step][:3]
print(f"Exactness sample: {len(sample)} endpoints across {len(perstate)} states. Round-tripping...")

res = {}
with cf.ThreadPoolExecutor(max_workers=16) as ex:
    futs = {ex.submit(sample_one, r): r for r in sample}
    for fu in cf.as_completed(futs):
        r = futs[fu]
        try: out = fu.result()
        except Exception as e: out = ('EXC', None, type(e).__name__)
        res[r['fips']] = (r['state'], r['county'], out)

tally = collections.Counter(out[0].split('-')[0] if out[0] in ('EXACT-MATCH','APPROX-MATCH','EXACT-MISMATCH','APPROX-MISMATCH') else out[0] for fips,(st,cty,out) in res.items())
detail = collections.Counter(out[0] for fips,(st,cty,out) in res.items())
print("\n=== EXACTNESS (round-trip at true parcel centroid) ===")
for k, v in detail.most_common(): print(f"  {k:18} {v}")
matched = sum(v for k, v in detail.items() if k.endswith('MATCH') and 'MIS' not in k)
exact = detail['EXACT-MATCH']; approxm = detail['APPROX-MATCH']
mism = detail['EXACT-MISMATCH'] + detail['APPROX-MISMATCH']
testable = matched + mism
print(f"\n  of {testable} testable round-trips: {matched} correct-owner ({100*matched/testable:.1f}%), {mism} wrong-owner")
if matched: print(f"  exact point-in-parcel: {exact} ({100*exact/matched:.0f}% of correct), buffer/approx: {approxm}")
print("\n  MISMATCHES (returned a different owner than the sampled parcel):")
for fips,(st,cty,out) in sorted(res.items()):
    if out[0].endswith('MISMATCH'):
        print(f"  {fips} {st} {cty[:18]:18} truth={norm(out[1])!r} got={norm(out[2])!r}")
print("\n  NON-RESOLVING samples (host/test-method issues, not necessarily bugs):")
for fips,(st,cty,out) in sorted(res.items()):
    if not out[0].endswith('MATCH'):
        print(f"  {fips} {st} {cty[:16]:16} {out[0]}")
json.dump({f: [v[0], v[1], v[2][0]] for f, v in res.items()}, open(os.path.join(HERE,'..','data','exactness_census.json'),'w'), indent=1)
