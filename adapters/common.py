"""Shared helpers for parcel OWNER-harvest adapters.

Normalized output schema (one row per parcel), written by every adapter so sources
compose into one national table:
  source, state, county, parcel_id, owner_name, value, lat, lon
"""
import os, csv, json, time, urllib.request, urllib.parse

NORM_FIELDS = ["source", "state", "county", "parcel_id", "owner_name", "value", "lat", "lon"]

def http_json(url, params=None, data=None, headers=None, tries=4, timeout=90):
    if params:
        url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    hdr = {"User-Agent": "parcel-owner-research/1.0", "Accept": "application/json"}
    hdr.update(headers or {})
    body = json.dumps(data).encode() if data is not None else None
    if body is not None:
        hdr.setdefault("Content-Type", "application/json")
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, data=body, headers=hdr,
                                         method="POST" if body is not None else "GET")
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except Exception as e:
            last = e; time.sleep(1.2 * (i + 1))
    raise last

def join_owner(*parts):
    seen, out = set(), []
    for p in parts:
        p = (p or "").strip()
        if p and p.upper() not in ("NONE", "NULL") and p not in seen:
            seen.add(p); out.append(p)
    return " / ".join(out)

def write_norm(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=NORM_FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in NORM_FIELDS})
    return path
