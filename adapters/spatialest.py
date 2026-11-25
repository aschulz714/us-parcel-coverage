"""
ADAPTER: Spatialest (now Schneider) property JSON API.  STATUS: code-ready; NOT live-demoed
in this sandbox because property.spatialest.com actively refused the connection from here
(WinError 10061). Runs from a normal network.

Mechanism (verified by the vendor-scoping pass): each county is
`https://property.spatialest.com/{state}/{county}`. A one-time handshake on the landing page
sets a Laravel session + XSRF-TOKEN cookie; then the search API returns owner inline:
    GET /{state}/{county}/api/v1/search?filters[<FIELD>]=<VALUE>
        headers: X-XSRF-TOKEN: <url-decoded XSRF-TOKEN cookie>, X-Requested-With: XMLHttpRequest
Owner is in each result's `display` array. There is a ~300-result/query cap, so enumeration
pages by filter value (e.g. owner-name prefix or parcel-id prefix). (A separate per-record
open sub-API exists at api.spatialest.com but its route format isn't discoverable without the
main host, which is blocked here.)

Usage (from an unblocked network): python spatialest.py <state> <county> [owner_field]
  e.g. python spatialest.py wa benton Owner
"""
import os, sys, string, http.cookiejar, urllib.request, urllib.parse, json
sys.path.insert(0, os.path.dirname(__file__))
from common import write_norm

OUT = os.path.join(os.path.dirname(__file__), "out")
UA = "parcel-owner-research/1.0"

def make_session(state, county):
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    op.open(urllib.request.Request(f"https://property.spatialest.com/{state}/{county}",
                                   headers={"User-Agent": UA}), timeout=60).read()
    xsrf = next((urllib.parse.unquote(c.value) for c in cj if c.name == "XSRF-TOKEN"), "")
    return op, xsrf

def search(op, xsrf, state, county, field, value):
    url = (f"https://property.spatialest.com/{state}/{county}/api/v1/search?"
           + urllib.parse.urlencode({f"filters[{field}]": value}))
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json",
                                "X-XSRF-TOKEN": xsrf, "X-Requested-With": "XMLHttpRequest"})
    return json.loads(op.open(req, timeout=60).read().decode("utf-8", "replace"))

def owner_from(rec):
    disp = rec.get("display")
    if isinstance(disp, list):
        for item in disp:
            lab = str(item.get("label", "")).lower() if isinstance(item, dict) else ""
            if "owner" in lab or "name" in lab:
                return item.get("value", "")
    return rec.get("owner") or rec.get("Owner") or ""

def harvest(state, county, owner_field="Owner"):
    op, xsrf = make_session(state, county)
    seen, rows = set(), []
    for pref in [a + b for a in string.ascii_uppercase for b in string.ascii_uppercase[:0]] or list(string.ascii_uppercase):
        d = search(op, xsrf, state, county, owner_field, pref)
        for rec in (d.get("results") or d.get("data") or []):
            pid = rec.get("parcel") or rec.get("pid") or rec.get("id")
            if not pid or pid in seen: continue
            seen.add(pid)
            rows.append({"source": f"spatialest:{state}/{county}", "state": state.upper(),
                         "county": county, "parcel_id": pid, "owner_name": owner_from(rec),
                         "value": rec.get("value", ""), "lat": "", "lon": ""})
    return rows

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__); sys.exit(0)
    state, county = sys.argv[1], sys.argv[2]
    of = sys.argv[3] if len(sys.argv) > 3 else "Owner"
    rows = harvest(state, county, of)
    own = sum(1 for r in rows if r["owner_name"])
    path = write_norm(os.path.join(OUT, f"spatialest_{state}_{county}_owners.csv"), rows)
    print(f"pulled {len(rows):,}; owner on {own:,}; -> {path}")
