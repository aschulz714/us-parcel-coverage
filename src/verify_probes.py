"""Automated verification net for arcgis owner claims in data/probes_west/<fips>.json.

Catches the main failure mode of cheaper probe-agents: claiming method=arcgis when the owner
field is actually blank/stripped (green-but-empty), a wrong-state collision, or a dead endpoint.
For each arcgis probe it LIVE-queries the recorded endpoint: total count, owner-populated count
(% populated), and a 3-row sample of real owner names. Flags anything suspicious for manual review.

Pure urllib/curl — NEAR-ZERO token cost (no model calls). Run this after EVERY sweep so every
arcgis claim is checked field-level, instead of sampling. (arcgis-join owner is off the query_url,
so those are skipped here — verify them via resolve.py.)

Usage:
  python src/verify_probes.py 46     # check all 46xxx (South Dakota) arcgis probes
  python src/verify_probes.py 13 27  # check GA + MN
  python src/verify_probes.py        # check ALL probes_west arcgis probes
"""
import sys, os, glob, json, re, ssl, urllib.request, urllib.parse, urllib.error

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROBE = os.path.join(ROOT, "data", "probes_west")
prefixes = sys.argv[1:] or [""]
_NOVERIFY = ssl._create_unverified_context()

def _open(url):
    req = urllib.request.Request(url, headers={"User-Agent":
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"})
    try:
        return urllib.request.urlopen(req, timeout=60)
    except urllib.error.URLError as e:
        # bad TLS chain (e.g. cherokeega.com) -> retry without cert verification, like resolve.py's _getj
        if isinstance(getattr(e, "reason", None), ssl.SSLError) or "SSL" in str(getattr(e, "reason", e)):
            return urllib.request.urlopen(req, timeout=60, context=_NOVERIFY)
        raise

def getj(url):
    return json.load(_open(url))

def count(qurl, where):
    u = qurl + "?" + urllib.parse.urlencode({"where": where, "returnCountOnly": "true", "f": "json"})
    try:
        j = getj(u)
        if isinstance(j, dict) and j.get("error"):
            return "API:" + str(j["error"].get("code", "?"))
        return j.get("count")
    except urllib.error.HTTPError as e:
        return f"HTTP{e.code}"
    except Exception as e:
        return f"ERR:{type(e).__name__}"

def sample(qurl, field, n=3):
    u = qurl + "?" + urllib.parse.urlencode(
        {"where": f"{field} IS NOT NULL AND {field} <> ''", "outFields": field,
         "resultRecordCount": n, "returnGeometry": "false", "f": "json"})
    try:
        d = getj(u)
        vals = [f.get("attributes", {}).get(field) for f in d.get("features", [])]
        return [str(v).strip() for v in vals if v not in (None, "")][:n] or ["(none returned)"]
    except urllib.error.HTTPError as e:
        return [f"HTTP{e.code}"]
    except Exception as e:
        return [f"ERR:{type(e).__name__}"]

files = []
for p in prefixes:
    files += glob.glob(os.path.join(PROBE, f"{p}*.json"))
flagged = []
checked = 0
for f in sorted(set(files)):
    try:
        j = json.load(open(f, encoding="utf-8"))
    except Exception:
        continue
    if j.get("method") != "arcgis":
        continue
    checked += 1
    fips, county = j.get("fips"), (j.get("county") or "")[:18]
    qurl = (j.get("query_url") or "").rstrip("/")
    of = next((t.strip() for t in re.split(r"[|,/]", j.get("owner_fields") or "") if t.strip()), "")
    if not qurl or not of:
        print(f"{fips} {county:18} SKIP (no query_url/owner_field)"); flagged.append(fips); continue
    tot = count(qurl, "1=1")
    pop = count(qurl, f"{of} IS NOT NULL AND {of} <> ''")
    pct = None
    if isinstance(tot, int) and isinstance(pop, int) and tot:
        pct = 100.0 * pop / tot
    s = sample(qurl, of)
    flag = ""
    if not isinstance(pop, int) or pop == 0:
        flag = " <<< ZERO/ERR OWNER -- REVIEW"
    elif pct is not None and pct < 20:
        flag = " <<< LOW OWNER % -- REVIEW"
    pcts = f"{pct:.0f}%" if pct is not None else "n/a"
    print(f"{fips} {county:18} {of[:24]:24} tot={tot} pop={pop} ({pcts}) {s}{flag}")
    if flag:
        flagged.append(fips)
print(f"\nchecked {checked} arcgis probes; {len(flagged)} flagged for review: {flagged}")
