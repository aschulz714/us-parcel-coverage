"""
ADAPTER: MapGeo (AppGeo/Sanborn) JSON search API.  STATUS: code-ready; not live-demoed
in this sandbox (see DISCOVERY note).

Mechanism (verified by the vendor-scoping pass): each MapGeo client is a subdomain
`https://{client}.mapgeo.io`. The per-dataset search API is PUBLIC (no auth):
    GET/POST /api/datasets/{dataset}/search?format=json   (query, page, perPage)
returns records whose owner field is present (key varies per client: `owner` / `ownerName`).
maxResults ~5000/query; there is NO blank-wildcard dump, so enumeration is driven by
search terms (parcel-id prefixes or A-Z). The detail endpoint /api/ui/datasets/{ds}/{id}
gives the full record. The DATASETS LISTING (/api/config/datasets) requires auth, so the
exact {dataset} name is the one discovery step.

DISCOVERY (the only blocker hit here): /api/datasets/{dataset}/search returns clean JSON,
but {dataset} must be the exact dataset name. /api/config/datasets returns 401
AUTHENTICATION_REQUIRED in this environment, and the name is not in the public HTML, so I
could not enumerate it from the sandbox. Supply the name (visible in the MapGeo app URL /
network calls, or via the authed config) and this runs as-is.

Usage: python mapgeo.py <client> <dataset> [state] [county]
  e.g. python mapgeo.py graysharborwa "Property" WA "Grays Harbor"
"""
import os, sys, string
sys.path.insert(0, os.path.dirname(__file__))
from common import http_json, write_norm

OUT = os.path.join(os.path.dirname(__file__), "out")
OWNER_KEYS = ("owner", "ownerName", "OWNER", "Owner", "owner_name")
VALUE_KEYS = ("totalValue", "assessedValue", "marketValue", "value", "TOTAL_VALUE")
ID_KEYS = ("parcelId", "parcel_id", "pid", "apn", "MAP_PAR_ID", "id")

def first(d, keys):
    for k in keys:
        if isinstance(d, dict) and d.get(k) not in (None, ""):
            return d.get(k)
    return ""

def search(client, dataset, term, per=500, page=1):
    url = f"https://{client}.mapgeo.io/api/datasets/{dataset}/search?format=json"
    return http_json(url, data={"query": term, "page": page, "perPage": per},
                     headers={"Accept": "application/json"})

def harvest(client, dataset, state="", county=""):
    seen, rows = set(), []
    # drive enumeration with single-char terms (no wildcard dump); dedup by id
    for term in list(string.digits) + list(string.ascii_uppercase):
        page = 1
        while True:
            d = search(client, dataset, term, per=500, page=page)
            recs = d.get("results") or d.get("records") or d.get("data") or []
            if not recs: break
            for rec in recs:
                props = rec.get("properties", rec)
                pid = first(props, ID_KEYS)
                if not pid or pid in seen: continue
                seen.add(pid)
                rows.append({"source": f"mapgeo:{client}", "state": state, "county": county,
                             "parcel_id": pid, "owner_name": first(props, OWNER_KEYS),
                             "value": first(props, VALUE_KEYS), "lat": "", "lon": ""})
            if len(recs) < 500: break
            page += 1
    return rows

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__); sys.exit(0)
    client, dataset = sys.argv[1], sys.argv[2]
    state = sys.argv[3] if len(sys.argv) > 3 else ""
    county = sys.argv[4] if len(sys.argv) > 4 else ""
    rows = harvest(client, dataset, state, county)
    own = sum(1 for r in rows if r["owner_name"])
    path = write_norm(os.path.join(OUT, f"mapgeo_{client}_owners.csv"), rows)
    print(f"pulled {len(rows):,}; owner on {own:,}; -> {path}")
