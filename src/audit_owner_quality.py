"""OWNER-CORRECTNESS census (2026-06-09). Plan A+C of the owner-quality audit.

For every UNIQUE resolvable arcgis endpoint:
  (1) fetch LAYER METADATA -> full field list (catches a better OWNER field we didn't pick)
  (2) total count + owner-POPULATED count  -> populated-rate (is the field mostly blank?)
  (3) sample ~12 features -> show the chosen-field values AND any competing OWNER-named field
This answers Q1 (field-selection: did we pick a taxpayer/care-of decoy when a true OWNER
field existed?) and the coverage sub-question (populated-rate). It is NOT a verdict on
deed-vs-source currency (Q4) -- that needs ground truth, done separately.

Dedup by query_url (a statewide layer shared by N counties is tested once).
Output: data/owner_quality_census.json + a console decomposition.
Usage: python src/audit_owner_quality.py
"""
import csv, json, ssl, gzip, urllib.request, urllib.parse, concurrent.futures as cf, collections, os

HERE = os.path.dirname(__file__)
CH = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36'}
ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE

def getj(url, t=25):
    last = 'err'
    for c in (None, ctx):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=CH), timeout=t, context=c) as r:
                b = r.read()
                if b[:2] == b'\x1f\x8b': b = gzip.decompress(b)
                return json.loads(b.decode('utf-8', 'replace'))
        except urllib.error.HTTPError as e:
            return {'__http__': e.code}
        except Exception as e:
            last = type(e).__name__
    return {'__err__': last}

def q(base, params): return getj(base.split('/query')[0] + '/query?' + urllib.parse.urlencode(params))
def meta(base): return getj(base.split('/query')[0] + '?f=json')

def first_field(s): return (s or '').replace('|', ',').split(',')[0].strip()
def all_fields(s): return [f.strip() for f in (s or '').replace('|', ',').split(',') if f.strip()]
def leaf(f): return f.split('.')[-1].strip()

# A field NAME that signals "true legal owner" (NOT taxpayer/mail/care/agent/contract).
def is_owner_named(name):
    n = leaf(name).lower()
    if any(t in n for t in ('taxpay', 'tax_nam', 'taxname', 'mail', 'care', 'c_o', 'agent', 'contract', 'attn', 'grantee', 'situs', 'addr')):
        return False
    return ('own' in n and 'town' not in n and 'down' not in n) or n in ('deedholder', 'deedname', 'deed_holder', 'grantor', 'name', 'name1', 'ownername', 'owner_name')

# Tokens that make the CHOSEN field a taxpayer/proxy rather than legal owner.
PROXY = ('care', 'c_o', 'agent', 'mail', 'contract', 'attn', 'dba', 'in_care', 'careof',
         'taxpay', 'tax_nam', 'taxname', 'tax_name', 'grantee')
def is_proxy_named(name):
    n = leaf(name).lower()
    return any(t in n for t in PROXY)

def owner_val(attrs, fields):
    for f in fields:
        v = attrs.get(f) or attrs.get(leaf(f))
        if v not in (None, '', ' ') and str(v).strip() not in ('', '0'):
            return str(v).strip()[:50]
    return None

def count(base, where):
    s = q(base, {'where': where, 'returnCountOnly': 'true', 'f': 'json'})
    if isinstance(s, dict) and 'count' in s: return s['count']
    return None

def census_endpoint(qurl, owner_fields):
    chosen = first_field(owner_fields)
    flds = all_fields(owner_fields)
    out = {'query_url': qurl, 'chosen_field': chosen}

    m = meta(qurl)
    if '__http__' in m or '__err__' in m:
        out['status'] = 'META-ERR'; out['detail'] = m.get('__http__') or m.get('__err__')
        return out
    layer_fields = [f.get('name') for f in (m.get('fields') or []) if f.get('name')]
    out['n_fields'] = len(layer_fields)
    # competing true-owner field on the layer that we did NOT choose
    competitors = [f for f in layer_fields if is_owner_named(f) and leaf(f).lower() != leaf(chosen).lower()]
    out['competing_owner_fields'] = competitors
    out['chosen_is_proxy'] = is_proxy_named(chosen)

    # total + populated count
    leaf_chosen = leaf(chosen)
    total = count(qurl, '1=1')
    pop = None
    for wf in (chosen, leaf_chosen):
        c = count(qurl, f"{wf} IS NOT NULL AND {wf} <> ''")
        if c is not None: pop = c; break
    if pop is None:  # some hosts reject <>'' -> just NOT NULL
        for wf in (chosen, leaf_chosen):
            c = count(qurl, f"{wf} IS NOT NULL")
            if c is not None: pop = c; break
    out['total'] = total; out['populated'] = pop
    out['pop_rate'] = round(pop / total, 3) if (total and pop is not None and total > 0) else None

    # sample features -> chosen value + competing value
    s = q(qurl, {'where': '1=1', 'outFields': '*', 'returnGeometry': 'false', 'resultRecordCount': 12, 'f': 'json'})
    samples = []
    comp_pop = 0
    feats = s.get('features') if isinstance(s, dict) else None
    for ft in (feats or []):
        a = ft.get('attributes', {})
        cv = owner_val(a, flds)
        comp_vals = {leaf(cf): (str(a.get(cf) or a.get(leaf(cf)) or '').strip()) for cf in competitors}
        if any(comp_vals.values()): comp_pop += 1
        samples.append({'chosen': cv, 'competitors': {k: v for k, v in comp_vals.items() if v}})
    out['samples'] = samples
    out['competitor_populated_in_sample'] = comp_pop
    out['status'] = 'OK'
    return out

# ---- load + dedupe by endpoint ----
rows = list(csv.DictReader(open(os.path.join(HERE, 'county_methods.csv'), encoding='utf-8')))
ag = [r for r in rows if r['method'] == 'arcgis']
byep = collections.OrderedDict()
for r in ag:
    byep.setdefault(r['query_url'], []).append(r)
print(f"resolvable arcgis: {len(ag)} counties on {len(byep)} unique endpoints. Census running...")

def run(qurl):
    rs = byep[qurl]
    try:
        res = census_endpoint(qurl, rs[0]['owner_fields'])
    except Exception as e:
        res = {'query_url': qurl, 'status': 'EXC', 'detail': type(e).__name__}
    res['counties'] = [(r['fips'], r['state'], r['county']) for r in rs]
    res['n_counties'] = len(rs)
    return qurl, res

results = {}
with cf.ThreadPoolExecutor(max_workers=24) as ex:
    for qurl, res in ex.map(run, list(byep.keys())):
        results[qurl] = res

json.dump(list(results.values()), open(os.path.join(HERE, '..', 'data', 'owner_quality_census.json'), 'w'), indent=1)

# ---------------- DECOMPOSITION ----------------
ok = [r for r in results.values() if r['status'] == 'OK']
err = [r for r in results.values() if r['status'] != 'OK']
def ctys(r): return r['n_counties']

print("\n=== ENDPOINT STATUS (by county count) ===")
sc = collections.Counter()
for r in results.values(): sc[r['status']] += ctys(r)
for k, v in sc.most_common(): print(f"  {k:10} {v}")

# 1) FIELD-SELECTION: competing true-owner field exists AND is populated in sample
print("\n=== Q1 FIELD-SELECTION: chosen a proxy/taxpayer while a true OWNER field is populated ===")
fix = [r for r in ok if r['chosen_is_proxy'] and r.get('competitor_populated_in_sample', 0) >= 6]
fix.sort(key=lambda r: (-ctys(r), r['counties'][0][1]))
fix_cty = sum(ctys(r) for r in fix)
print(f"  FIX CANDIDATES: {len(fix)} endpoints / {fix_cty} counties")
for r in fix:
    c0 = r['counties'][0]
    print(f"  {c0[0]} {c0[1]} {c0[2][:20]:20} chose {r['chosen_field']:24} BETTER:{','.join(r['competing_owner_fields'])[:40]}  (n={ctys(r)})")

# 2) proxy field but NO competing owner field -> documentation caveat (taxpayer is best available)
print("\n=== Q1 PROXY-BEST: taxpayer/proxy field chosen, NO true OWNER field on layer (caveat, not bug) ===")
proxy_best = [r for r in ok if r['chosen_is_proxy'] and not r['competing_owner_fields']]
pb_cty = sum(ctys(r) for r in proxy_best)
print(f"  {len(proxy_best)} endpoints / {pb_cty} counties (label as taxpayer-proxy)")
for r in sorted(proxy_best, key=lambda r: (r['counties'][0][1], r['counties'][0][0])):
    c0 = r['counties'][0]
    print(f"  {c0[0]} {c0[1]} {c0[2][:20]:20} {r['chosen_field']:24} pop_rate={r['pop_rate']}  (n={ctys(r)})")

# 3) POPULATED-RATE distribution
print("\n=== COVERAGE: owner-populated-rate distribution (by county count) ===")
buckets = collections.Counter(); unknown = 0
for r in ok:
    pr = r['pop_rate']
    if pr is None: unknown += ctys(r); continue
    b = '>=0.95' if pr >= 0.95 else '0.85-0.95' if pr >= 0.85 else '0.50-0.85' if pr >= 0.50 else '<0.50'
    buckets[b] += ctys(r)
for b in ('>=0.95', '0.85-0.95', '0.50-0.85', '<0.50'):
    print(f"  {b:10} {buckets[b]}")
print(f"  rate-unknown(host rejected count) {unknown}")
print("\n  LOW-populated endpoints (<0.85, owner mostly blank):")
low = [r for r in ok if r['pop_rate'] is not None and r['pop_rate'] < 0.85]
for r in sorted(low, key=lambda r: r['pop_rate']):
    c0 = r['counties'][0]
    print(f"  {c0[0]} {c0[1]} {c0[2][:20]:20} {r['chosen_field']:22} pop_rate={r['pop_rate']} total={r['total']}  (n={ctys(r)})")

print(f"\n=== ENDPOINT ERRORS: {sum(ctys(r) for r in err)} counties ===")
for r in sorted(err, key=lambda r: r['counties'][0][1]):
    c0 = r['counties'][0]
    print(f"  {c0[0]} {c0[1]} {c0[2][:18]:18} {r['status']}:{r.get('detail')}  (n={ctys(r)})")
