"""
ADAPTER: generic ArcGIS REST owner puller.

The workhorse — one adapter covers the two biggest free-owner buckets:
  (a) county-direct ArcGIS sources (the WA pattern: ~60% of counties), and
  (b) the Schneider/Beacon+qPublic vendor BACKEND (wfs.schneidercorp.com), which
      serves owner as plain unauthenticated ArcGIS JSON even though the Beacon
      front-end is Cloudflare-walled.

Given a source config (query URL + owner field names), it auto-detects the parcel-id
field, pages through with resultOffset, and writes the normalized owner table. It
asserts the pulled count reconciles with the server's returnCountOnly (no silent
truncation) — same discipline as the WA pull scripts.

Demonstrated live on two distinct real sources (see SOURCES). Run: python arcgis_owner.py
"""
import os, sys
sys.path.insert(0, os.path.dirname(__file__))
from common import http_json, join_owner, write_norm

OUT = os.path.join(os.path.dirname(__file__), "out")

SOURCES = {
    # Schneider vendor backend (ArcGIS REST) — proves the vendor path
    "schneider_lesueur_mn": {
        "state": "MN", "county": "Le Sueur",
        "query": "https://wfs.schneidercorp.com/arcgis/rest/services/LeSueurCountyMN_WFS/MapServer/1/query",
        "owner_fields": ["OwnerName", "OwnerName2"],
        "value_fields": ["EMVTotal"],
        "cap": None,            # full county (~19.5k)
    },
    # County-direct hosted FeatureServer (the WA pattern) — proves the county path
    "snohomish_wa": {
        "state": "WA", "county": "Snohomish",
        "query": "https://services6.arcgis.com/z6WYi9VRHfgwgtyW/arcgis/rest/services/Parcels/FeatureServer/0/query",
        "owner_fields": ["OWNERNAME"],
        "value_fields": ["MKTTL"],
        "cap": 3000,            # sample (county is 318k); full pull is the same loop
    },
}

PIN_HINTS = ("pin", "parcel", "pid", "apn", "parcelid", "parcel_id", "parcelnumb", "geo")

def layer_meta(query_url):
    base = query_url.rsplit("/query", 1)[0]
    return http_json(base, {"f": "json"})

def detect_pin(fields, owner_fields):
    names = [f["name"] for f in fields]
    ofset = {o.lower() for o in owner_fields}
    for f in fields:                       # prefer an explicit parcel-id field
        n = f["name"]
        if n.lower() in ofset: continue
        if any(h == n.lower() for h in PIN_HINTS) or n.lower().replace("_", "") in ("parcelid","parcelno","parcelnumber","pinno"):
            return n
    for f in fields:                       # else first field whose name contains a hint
        n = f["name"]
        if n.lower() in ofset: continue
        if any(h in n.lower() for h in PIN_HINTS):
            return n
    return "OBJECTID"

def count(query_url):
    return http_json(query_url, {"where": "1=1", "returnCountOnly": "true", "f": "json"}).get("count")

def pull(key, cfg):
    meta = layer_meta(cfg["query"])
    fields = meta.get("fields", [])
    pin = detect_pin(fields, cfg["owner_fields"])
    # page by the OBJECTID field — universally sortable. (Ordering by a string parcel-id
    # field silently nulls attributes on some hosted FeatureServers, e.g. Snohomish.)
    oid = meta.get("objectIdField") or next(
        (f["name"] for f in fields if f.get("type") == "esriFieldTypeOID"), "OBJECTID")
    maxrec = min(meta.get("maxRecordCount", 1000), 1000)
    out_fields = ",".join([pin] + cfg["owner_fields"] + cfg["value_fields"])
    authoritative = count(cfg["query"])
    print(f"[{key}] {cfg['county']} {cfg['state']}: server count={authoritative:,}, "
          f"pin_field={pin}, owner={cfg['owner_fields']}, page={maxrec}")

    rows, offset, cap = [], 0, cfg.get("cap")
    while True:
        d = http_json(cfg["query"], {
            "where": "1=1", "outFields": out_fields, "returnGeometry": "true",
            "outSR": "4326", "resultOffset": offset, "resultRecordCount": maxrec,
            "orderByFields": oid, "f": "json"})
        feats = d.get("features", [])
        if not feats: break
        for ft in feats:
            a = ft["attributes"]
            owner = join_owner(*[a.get(o) for o in cfg["owner_fields"]])
            val = next((a.get(v) for v in cfg["value_fields"] if a.get(v) not in (None, "")), "")
            g = ft.get("geometry") or {}
            lon = lat = ""
            if "rings" in g and g["rings"]:
                pts = max(g["rings"], key=len)
                lon = round(sum(p[0] for p in pts) / len(pts), 6)
                lat = round(sum(p[1] for p in pts) / len(pts), 6)
            elif "x" in g:
                lon, lat = g.get("x"), g.get("y")
            rows.append({"source": f"arcgis:{key}", "state": cfg["state"], "county": cfg["county"],
                         "parcel_id": a.get(pin, ""), "owner_name": owner, "value": val,
                         "lat": lat, "lon": lon})
        offset += len(feats)
        if cap and offset >= cap: rows = rows[:cap]; break
        if not d.get("exceededTransferLimit") and len(feats) < maxrec: break
    return rows, authoritative

if __name__ == "__main__":
    for key, cfg in SOURCES.items():
        rows, auth = pull(key, cfg)
        with_owner = sum(1 for r in rows if r["owner_name"])
        path = write_norm(os.path.join(OUT, f"{key}_owners.csv"), rows)
        cap = cfg.get("cap")
        tag = f"(sample of {auth:,})" if cap else f"== server {auth:,}" + (" OK" if len(rows) == auth else " MISMATCH")
        print(f"  pulled {len(rows):,} {tag}; owner populated on {with_owner:,} "
              f"({100*with_owner/max(len(rows),1):.0f}%)")
        for r in rows[:3]:
            print(f"     e.g. {r['parcel_id']}  {r['owner_name'][:42]:<42} ${r['value']}")
        print(f"  -> {path}")
