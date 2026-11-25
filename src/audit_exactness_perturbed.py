"""Plan B, REALISTIC: perturbed-point exactness. The centroid round-trip proves SR/point-in-polygon/
field-extraction work, but it drops the point INSIDE the parcel, so it can never trigger the buffer/
envelope fallback that is the only path to a NEIGHBOR's owner. Production input is a geocoded facility
address that lands OFF-parcel. So: offset the truth-parcel centroid by ~OFFSET meters on a deterministic
bearing (simulating geocode error where the intended answer is still the original parcel), resolve there,
and measure how often the resolver then returns a DIFFERENT owner than the truth parcel.

Interpretation: with a realistic offset, the 'correct' answer is the original parcel's owner. A different
owner is an error (the point drifted into / the buffer grabbed a neighbor). This is an UPPER bound -- some
drifted points genuinely fall inside a neighbor, where returning that neighbor is arguably right.
Usage: python src/audit_exactness_perturbed.py [OFFSET_METERS]
"""
import csv, json, ssl, gzip, math, urllib.request, urllib.parse, concurrent.futures as cf, collections, os, sys
sys.path.insert(0, os.path.dirname(__file__))
from resolve import resolve

HERE = os.path.dirname(__file__)
CH = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36'}
ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
OFFSET = float(sys.argv[1]) if len(sys.argv) > 1 else 50.0

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
    pts = max(rings, key=len); xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
    return sum(xs) / len(xs), sum(ys) / len(ys)

def offset_point(lon, lat, meters, bearing_deg):
    br = math.radians(bearing_deg)
    dlat = (meters * math.cos(br)) / 111320.0
    dlon = (meters * math.sin(br)) / (111320.0 * max(0.2, math.cos(math.radians(lat))))
    return lon + dlon, lat + dlat

def sample_one(row):
    own = first(row['owner_fields']); base = row['query_url'].split('/query')[0]
    s = getj(base + '/query?' + urllib.parse.urlencode({
        'where': f"{own} LIKE '% %'", 'outFields': '*', 'returnGeometry': 'true', 'outSR': 4326,
        'orderByFields': 'OBJECTID DESC', 'resultRecordCount': 1, 'f': 'json'}))
    feats = s.get('features') if isinstance(s, dict) else None
    if not feats: return ('NO-SAMPLE', None, None)
    f0 = feats[0]
    # full owner per crosswalk (all pipe fields), not just first
    truth = ' / '.join(v for fld in row['owner_fields'].split('|')
                       for v in [' '.join(str(f0['attributes'].get(fld) or f0['attributes'].get(fld.split('.')[-1]) or '').split())] if v)
    c = centroid(f0.get('geometry') or {})
    if not c: return ('NO-GEOM', truth, None)
    lon, lat = c
    blon, blat = offset_point(lon, lat, OFFSET, (hash(row['fips']) % 360))
    try:
        res = resolve(lat=blat, lon=blon)
    except Exception as e:
        return ('RESOLVE-EXC', truth, type(e).__name__)
    if res.get('status') != 'resolved':
        return ('MISS-' + str(res.get('status')), truth, res.get('owner'))
    got = res.get('owner'); approx = res.get('approx')
    match = bool(norm(got)) and bool(norm(truth)) and (norm(got) == norm(truth) or norm(got) in norm(truth) or norm(truth) in norm(got))
    if match:
        return ('CORRECT-EXACT' if not approx else 'CORRECT-BUFFER', truth, got)
    return ('WRONG-EXACT' if not approx else 'WRONG-BUFFER', truth, got)

rows = list(csv.DictReader(open(os.path.join(HERE, 'county_methods.csv'), encoding='utf-8')))
ag = [r for r in rows if r['method'] == 'arcgis']
byep = collections.OrderedDict()
for r in ag: byep.setdefault(r['query_url'], r)
perstate = collections.defaultdict(list)
for r in byep.values(): perstate[r['state']].append(r)
sample = []
for st, rs in perstate.items():
    step = max(1, len(rs) // 4)
    sample += rs[::step][:4]
print(f"Perturbed exactness @ {OFFSET:.0f}m: {len(sample)} endpoints, {len(perstate)} states. Resolving offset points...")

res = {}
with cf.ThreadPoolExecutor(max_workers=16) as ex:
    futs = {ex.submit(sample_one, r): r for r in sample}
    for fu in cf.as_completed(futs):
        r = futs[fu]
        try: out = fu.result()
        except Exception as e: out = ('EXC', None, str(e))
        res[r['fips']] = (r['state'], r['county'], out)

detail = collections.Counter(out[0] for _, (_, _, out) in res.items())
print(f"\n=== PERTURBED EXACTNESS @ {OFFSET:.0f}m offset ===")
for k, v in detail.most_common(): print(f"  {k:22} {v}")
correct = detail['CORRECT-EXACT'] + detail['CORRECT-BUFFER']
wrong = detail['WRONG-EXACT'] + detail['WRONG-BUFFER']
testable = correct + wrong
if testable:
    print(f"\n  of {testable} testable offset points: {correct} correct-owner ({100*correct/testable:.1f}%), {wrong} different-owner ({100*wrong/testable:.1f}%)")
    print(f"  still-exact-after-offset: {detail['CORRECT-EXACT']}  |  buffer-rescued-correct: {detail['CORRECT-BUFFER']}  |  buffer-grabbed-neighbor: {detail['WRONG-BUFFER']}  |  drifted-into-neighbor(exact): {detail['WRONG-EXACT']}")
print("\n  DIFFERENT-OWNER cases (offset point returned a non-truth owner -- some legitimately neighbor):")
for fips, (st, cty, out) in sorted(res.items()):
    if out[0].startswith('WRONG'):
        print(f"  {fips} {st} {cty[:16]:16} {out[0]:13} truth={norm(out[1])!r} got={norm(out[2])!r}")
json.dump({f: [v[0], v[2][0]] for f, v in res.items()}, open(os.path.join(HERE, '..', 'data', f'exactness_perturbed_{int(OFFSET)}.json'), 'w'), indent=1)
