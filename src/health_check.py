"""ENDPOINT HEALTH MONITOR — keep the crosswalk from silently decaying.

The crosswalk points at ~880 live third-party endpoints (county/state ArcGIS + GeoServer
hosts). They drift: counties migrate vendors, layers get deprecated, hosts add token gates.
This script pings every UNIQUE resolvable endpoint, classifies it ALIVE / GATED / DEAD /
UNREACHABLE, diffs against a stored baseline, and reports the counties that NEWLY broke since
last run — so "2,343 resolvable" stays a fact, not a stale claim.

Liveness is a fast metadata+count probe (not a full point-query round-trip). For a county the
monitor flags DEAD/GATED, deep-confirm with audit_pointquery.py before re-routing it.

Usage:
  python health_check.py                 # check all, diff vs baseline, update baseline, print report
  python health_check.py --baseline      # (re)establish the baseline, no diff (use after a known-good rebuild)
  python health_check.py --json out.json # also dump full per-endpoint results
  python health_check.py --ci            # CI mode: do NOT update the baseline; exit 1 if anything newly broke
                                         # (used by .github/workflows/health-check.yml so a red run = drift alert)
Baseline: data/health_baseline.json (commit it so the baseline travels with the repo).
"""
import os, sys, csv, json, ssl, gzip, urllib.request, urllib.parse, urllib.error
import concurrent.futures as cf, collections, datetime

HERE = os.path.dirname(__file__)
CROSSWALK = os.path.join(HERE, "county_methods.csv")
BASELINE = os.path.join(HERE, "..", "data", "health_baseline.json")
CH = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"}
_ctx = ssl.create_default_context(); _ctx.check_hostname = False; _ctx.verify_mode = ssl.CERT_NONE

def _get(url, timeout=25):
    """Return (json_or_None, http_status_or_errname). Tries verified then unverified TLS."""
    for ctx in (None, _ctx):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=CH), timeout=timeout, context=ctx) as r:
                b = r.read()
                if b[:2] == b"\x1f\x8b": b = gzip.decompress(b)
                try: return json.loads(b.decode("utf-8", "replace")), 200
                except json.JSONDecodeError: return None, "non-json"
        except urllib.error.HTTPError as e:
            return None, e.code
        except Exception as e:
            last = type(e).__name__
    return None, last

# error codes/strings that mean "the layer is there but you need a token / aren't allowed"
GATED_CODES = {403, 498, 499}
def _classify_arcgis(base):
    meta, st = _get(base.split("/query")[0] + "?f=json")
    if meta is None:
        if st in GATED_CODES: return "GATED", str(st)
        if isinstance(st, int) and st in (404, 410): return "DEAD", str(st)
        return "UNREACHABLE", str(st)
    err = meta.get("error") if isinstance(meta, dict) else None
    if err:
        code = err.get("code")
        return ("GATED" if code in GATED_CODES else "DEAD"), f"err{code}"
    # a real layer has fields/name/type; a moved service often returns a folder/service desc instead
    if isinstance(meta, dict) and (meta.get("fields") or meta.get("type") or meta.get("geometryType")):
        return "ALIVE", "ok"
    return "MOVED", "no-layer-fields"

def _classify_wfs(qurl):
    # okmaps GeoServer: fetch one feature; a live WFS returns a FeatureCollection
    u = qurl.split("?")[0] + "?" + urllib.parse.urlencode({
        "service": "WFS", "version": "2.0.0", "request": "GetFeature",
        "typeNames": "current_statewide_parcels", "outputFormat": "application/json", "count": 1})
    d, st = _get(u)
    if d is None:
        if st in GATED_CODES: return "GATED", str(st)
        return ("DEAD" if st in (404, 410) else "UNREACHABLE"), str(st)
    if isinstance(d, dict) and (d.get("features") is not None or d.get("type") == "FeatureCollection"):
        return "ALIVE", "ok"
    return "MOVED", "no-features"

def _classify(method, qurl, jurl):
    if method == "wfs":
        return _classify_wfs(qurl)
    status, detail = _classify_arcgis(qurl)
    if status == "ALIVE" and method == "arcgis-join" and jurl:   # owner lives in the join table -> it must live too
        js, jd = _classify_arcgis(jurl)
        if js != "ALIVE": return ("DEAD" if js in ("DEAD", "MOVED") else js, "jointable:" + jd)
    return status, detail

def check(sig):
    method, qurl, jurl = sig
    status, detail = _classify(method, qurl, jurl)
    # Retry once on a non-ALIVE result: transient timeouts/blips shouldn't read as decay.
    # (A genuine break -- e.g. a removed layer -- stays broken on retry; see St. Charles MO.)
    if status != "ALIVE":
        status, detail = _classify(method, qurl, jurl)
    return sig, (status, detail)

def load_rows():
    rows = list(csv.DictReader(open(CROSSWALK, encoding="utf-8")))
    def resolvable(r):
        return r["method"] in ("arcgis", "wfs") or (r["method"] == "arcgis-join" and (r.get("join_url") or "").strip())
    return [r for r in rows if resolvable(r)]

def main():
    rows = load_rows()
    byep = collections.OrderedDict()
    for r in rows:
        sig = (r["method"], r["query_url"], r.get("join_url", ""))
        byep.setdefault(sig, []).append(r)
    print(f"Resolvable counties: {len(rows)} on {len(byep)} unique endpoints. Pinging...")

    results = {}
    with cf.ThreadPoolExecutor(max_workers=24) as ex:
        for sig, res in ex.map(check, list(byep.keys())):
            results[sig] = res

    # tally by county
    bycty = collections.Counter()
    for sig, (status, _) in results.items(): bycty[status] += len(byep[sig])
    print("\n=== ENDPOINT HEALTH (by county) ===")
    for k in ("ALIVE", "GATED", "DEAD", "MOVED", "UNREACHABLE"):
        if bycty[k]: print(f"  {k:12} {bycty[k]}")

    # key string per endpoint for the baseline (method+url; join_url folded in)
    def key(sig): return f"{sig[0]}|{sig[1]}|{sig[2]}"
    current = {key(sig): {"status": results[sig][0], "detail": results[sig][1],
                          "counties": [r["fips"] for r in byep[sig]]} for sig in byep}

    if "--baseline" in sys.argv:
        json.dump(current, open(BASELINE, "w"), indent=1)
        print(f"\nBaseline established: {len(current)} endpoints -> {BASELINE}")
        return

    base = json.load(open(BASELINE)) if os.path.exists(BASELINE) else None
    if base is None:
        json.dump(current, open(BASELINE, "w"), indent=1)
        print(f"\nNo baseline found -> established one ({len(current)} endpoints). Re-run later to detect drift.")
        return

    newly_broken, recovered = [], []
    for k, cur in current.items():
        was = (base.get(k) or {}).get("status")
        if was == "ALIVE" and cur["status"] != "ALIVE":
            newly_broken.append((k, cur))
        elif was and was != "ALIVE" and cur["status"] == "ALIVE":
            recovered.append((k, cur))

    fips2name = {r["fips"]: (r["county"], r["state"]) for r in rows}
    print(f"\n=== DRIFT vs baseline ({len(base)} endpoints) ===")
    n_cty = sum(len(c["counties"]) for _, c in newly_broken)
    print(f"NEWLY BROKEN endpoints: {len(newly_broken)}  (= {n_cty} counties that no longer resolve)")
    for k, c in sorted(newly_broken, key=lambda x: -len(x[1]["counties"])):
        host = k.split("|")[1].split("/")[2] if "//" in k else k
        ctys = ", ".join(f"{fips2name.get(f,('?','?'))[0]} {fips2name.get(f,('?','?'))[1]}" for f in c["counties"][:6])
        more = "" if len(c["counties"]) <= 6 else f" +{len(c['counties'])-6} more"
        print(f"  [{c['status']}/{c['detail']}] {host}  ({len(c['counties'])} cty: {ctys}{more})")
    print(f"\nRECOVERED endpoints: {len(recovered)}")
    for k, c in recovered[:20]:
        host = k.split("|")[1].split("/")[2] if "//" in k else k
        print(f"  {host}  ({len(c['counties'])} cty)")

    if "--ci" in sys.argv:
        print("\n--ci: baseline NOT updated (update it from a local run after triaging).")
    else:
        json.dump(current, open(BASELINE, "w"), indent=1)
        stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        print(f"\nBaseline updated ({stamp}). NEXT: deep-confirm NEWLY-BROKEN with audit_pointquery.py before re-routing.")
    for a in sys.argv:
        if a.endswith(".json") and a != "--json":
            json.dump(current, open(a, "w"), indent=1); print(f"Full results -> {a}")
    if "--ci" in sys.argv and newly_broken:
        sys.exit(1)  # red CI run = the drift alert (GitHub emails the repo owner)

if __name__ == "__main__":
    main()
