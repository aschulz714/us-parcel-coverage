"""Ad-hoc REST probe helper, mirroring resolve.py mechanics (browser UA, gunzip, SSL fallback).
Used for IN/AL/IL/SC regional-host recon. Not part of the resolver.
Adds a `cascade` cmd that mirrors resolve.py's full point-query fallback chain
(exact -> buffer40 -> envelope -> native-SR reproject) so a pass here == production resolver."""
import json, ssl, sys, urllib.request, urllib.parse, gzip, io

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
      "Accept": "*/*"}
_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE

def get(url, params=None, timeout=40):
    if params:
        url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=UA)
    try:
        r = urllib.request.urlopen(req, timeout=timeout, context=_CTX)
        raw = r.read()
        if r.headers.get("Content-Encoding") == "gzip" or raw[:2] == b"\x1f\x8b":
            raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
        return raw.decode("utf-8", "replace")
    except Exception as e:
        return f"__ERR__ {type(e).__name__}: {e}"

def getj(url, params=None, timeout=40):
    t = get(url, params, timeout)
    if t.startswith("__ERR__"):
        return {"__err__": t}
    try:
        return json.loads(t)
    except Exception as e:
        return {"__err__": f"JSONParse: {e}", "__head__": t[:300]}

def pq(query_url, lon, lat, dist=None):
    """Point query, esriGeometryPoint inSR=4326."""
    p = {"geometry": f"{lon},{lat}", "geometryType": "esriGeometryPoint", "inSR": "4326",
         "spatialRel": "esriSpatialRelIntersects", "outFields": "*",
         "returnGeometry": "false", "f": "json"}
    if dist:
        p.update(distance=dist, units="esriSRUnit_Meter")
    return getj(query_url, p)

def pq_env(query_url, lon, lat, half=0.0008):
    p = {"geometry": f"{lon-half},{lat-half},{lon+half},{lat+half}",
         "geometryType": "esriGeometryEnvelope", "inSR": "4326",
         "spatialRel": "esriSpatialRelIntersects", "outFields": "*",
         "returnGeometry": "false", "f": "json"}
    return getj(query_url, p)

def pq_native(query_url, lon, lat):
    """Host ignores inSR=4326: reproject point to layer native SR (handles ESRI 102xxx wkids)."""
    try:
        import pyproj
    except Exception as e:
        return {"__err__": f"no-pyproj: {e}"}
    meta = getj(query_url.split("/query")[0])
    if "__err__" in meta:
        return meta
    sr = (meta.get("extent", {}) or {}).get("spatialReference", {}) or meta.get("spatialReference", {}) or {}
    wkid = sr.get("latestWkid") or sr.get("wkid")
    if not wkid or int(wkid) in (4326, 4269, 3857, 102100):
        return {"__skip__": f"native SR wkid={wkid} already lon/lat-friendly"}
    try:
        tr = pyproj.Transformer.from_crs(4326, int(wkid), always_xy=True)
    except Exception:
        tr = pyproj.Transformer.from_crs("EPSG:4326", f"ESRI:{int(wkid)}", always_xy=True)
    x, y = tr.transform(lon, lat)
    for dist in (None, 60):
        p = {"geometry": f"{x},{y}", "geometryType": "esriGeometryPoint", "inSR": str(wkid),
             "spatialRel": "esriSpatialRelIntersects", "outFields": "*",
             "returnGeometry": "false", "f": "json"}
        if dist: p.update(distance=dist, units="esriSRUnit_Meter")
        d = getj(query_url, p)
        if d.get("features"):
            d["__wkid__"] = wkid
            return d
    return {"__err__": f"native-SR(wkid={wkid}) no features", "__wkid__": wkid}

def _feats(d):
    return d.get("features", []) if isinstance(d, dict) else []

def cascade(query_url, lon, lat, owner_fields=""):
    """Full resolve.py cascade. Prints stage that hit + owner (or full attrs if owner blank)."""
    def owner_of(a):
        vals = []
        for fld in (owner_fields or "").split("|"):
            v = " ".join(str(a.get(fld) or "").split())
            if v and v not in vals: vals.append(v)
        return " / ".join(vals) or None

    stages = [("exact", lambda: pq(query_url, lon, lat)),
              ("buffer40", lambda: pq(query_url, lon, lat, 40)),
              ("envelope", lambda: pq_env(query_url, lon, lat)),
              ("native-SR", lambda: pq_native(query_url, lon, lat))]
    for name, fn in stages:
        d = fn()
        if isinstance(d, dict) and d.get("__err__"):
            print(f"  [{name}] err: {str(d['__err__'])[:160]}")
            continue
        fs = _feats(d)
        if not fs:
            continue
        chosen = None
        for ft in fs:
            if owner_of(ft.get("attributes", {})):
                chosen = ft; break
        if chosen is None:
            chosen = fs[0]
        a = chosen.get("attributes", {})
        own = owner_of(a)
        extra = f" (native wkid={d.get('__wkid__')})" if d.get("__wkid__") else ""
        if own:
            print(f"  STAGE={name}{extra}  OWNER={own!r}")
        else:
            print(f"  STAGE={name}{extra}  parcel found but owner field blank. ALL ATTRS:")
            for k, v in a.items():
                print(f"      {k} = {v!r}")
        return
    print(f"  NO PARCEL at ({lat},{lon}) after all 4 stages")

if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "get":
        print(get(sys.argv[2]))
    elif cmd == "getj":
        print(json.dumps(getj(sys.argv[2]), indent=1)[:6000])
    elif cmd == "pq":
        d = float(sys.argv[5]) if len(sys.argv) > 5 else None
        print(json.dumps(pq(sys.argv[2], float(sys.argv[3]), float(sys.argv[4]), d), indent=1)[:4000])
    elif cmd == "cascade":
        # cascade <query_url> <lon> <lat> <owner_fields>
        cascade(sys.argv[2], float(sys.argv[3]), float(sys.argv[4]),
                sys.argv[5] if len(sys.argv) > 5 else "")
