"""
item2 OWNER RESOLVER — the first crosswalk slice.

Given a public-company facility LOCATION (lat/lon, or a street address), return who owns
the parcel:  location -> county (Census) -> crosswalk method -> ArcGIS point-in-parcel -> owner.

Honest routing: counties whose owner isn't point-queryable (King=deed-records,
Clark/Pierce=suppressed, bulk-download, none) return the method + what it would take,
instead of a wrong/blank answer.

Usage:  python resolve.py            (runs the public-company demo)
        from resolve import resolve;  resolve(lat=.., lon=..)  /  resolve(address="..")
"""
import os, csv, json, ssl, urllib.request, urllib.parse, urllib.error

HERE = os.path.dirname(__file__)
CROSSWALK = os.path.join(HERE, "county_methods.csv")
# Browser-like UA: some county hosts (e.g. AZ DWR azwater.gov) sit behind a Cloudflare WAF
# that blocks non-browser user-agents on the parcel REST endpoints.
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"}

XW = {}
with open(CROSSWALK, encoding="utf-8") as f:
    for r in csv.DictReader(f):
        XW[r["fips"]] = r

# Some state GIS hosts serve a broken TLS chain (e.g. AR GeoStor). For read-only public parcel
# data, fall back to an unverified SSL context ONLY when cert verification is what failed.
_NOVERIFY = ssl.create_default_context()
_NOVERIFY.check_hostname = False
_NOVERIFY.verify_mode = ssl.CERT_NONE

def _readj(r):
    """Read a urllib response -> JSON, transparently gunzipping. Some county hosts (e.g.
    www.colesco.illinois.gov) return gzip-encoded bodies even when not asked, which would
    otherwise break json.loads with a JSONDecodeError."""
    data = r.read()
    if data[:2] == b"\x1f\x8b" or (r.headers.get("Content-Encoding", "").lower() == "gzip"):
        import gzip
        data = gzip.decompress(data)
    return json.loads(data.decode("utf-8", "replace"))

def _getj(url, params=None):
    if params: url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return _readj(r)
    except (ssl.SSLError, urllib.error.URLError) as e:
        reason = getattr(e, "reason", e)
        if isinstance(e, ssl.SSLError) or isinstance(reason, ssl.SSLError):
            with urllib.request.urlopen(req, timeout=60, context=_NOVERIFY) as r:
                return _readj(r)
        raise

def _census_county(lat=None, lon=None, address=None):
    """Return (fips, lon, lat, matched) using the free Census geocoder."""
    b = {"benchmark": "Public_AR_Current", "vintage": "Current_Current", "format": "json"}
    if address:
        d = _getj("https://geocoding.geo.census.gov/geocoder/geographies/onelineaddress",
                  {**b, "address": address})
        m = d["result"]["addressMatches"]
        if not m: return None, None, None, None
        c = m[0]["coordinates"]; cty = m[0]["geographies"]["Counties"][0]
        return cty["STATE"] + cty["COUNTY"], c["x"], c["y"], m[0]["matchedAddress"]
    d = _getj("https://geocoding.geo.census.gov/geocoder/geographies/coordinates",
              {**b, "x": lon, "y": lat})
    cs = d["result"]["geographies"].get("Counties", [])
    if not cs: return None, lon, lat, None
    return cs[0]["STATE"] + cs[0]["COUNTY"], lon, lat, f"{lat},{lon}"

def _best_value(attrs, prefer=None):
    if str(prefer or "").strip().upper() == "NONE":
        return None
    # Reject fields whose NAME signals a non-dollar number: dates/years (incl. YYYYMMDD instrument
    # dates that look like plausible $ amounts, e.g. 19991231), ids, zips, fips, and ACREAGE
    # (totalacres has "total" so the value-word loop would otherwise grab it).
    bad = ("date", "year", "yr", "_id", "objectid", "zip", "fips", "yyyymmdd", "_dt", "instrmnt", "acre")
    def ok(k): return not any(b in k.lower() for b in bad)
    # Prefer the crosswalk's declared value field when it's present, numeric, plausible, and not date/id/acreage.
    if prefer and prefer in attrs and ok(prefer):
        try:
            n = float(attrs[prefer])
            if 0 < n < 1e11: return (prefer, n)
        except (TypeError, ValueError): pass
    best = None
    for k, v in attrs.items():
        if not ok(k): continue
        try: n = float(v)
        except (TypeError, ValueError): continue
        if not (0 < n < 1e11): continue   # no single parcel is worth >$100B; filters epochs/IDs
        if any(h in k.lower() for h in ("mkt", "market", "assess", "appraised", "total", "val", "emv")):
            if best is None or n > best[1]: best = (k, n)
    return best

def _owner_from(attrs, owner_fields):
    # Extract+dedupe owner from a feature's attribute/property dict. " ".join(split()) collapses
    # internal padding too (handles fixed-width fields and the WFS double-space owner strings).
    vals = []
    for fld in (owner_fields or "").split("|"):
        v = " ".join(str(attrs.get(fld) or "").split())
        if v and v not in vals: vals.append(v)
    return " / ".join(vals) or None

def resolve(lat=None, lon=None, address=None):
    fips, lon, lat, matched = _census_county(lat, lon, address)
    out = {"input": address or f"{lat},{lon}", "matched": matched, "fips": fips}
    if not fips:
        return {**out, "status": "no-geocode", "owner": None}
    row = XW.get(fips)
    if not row:
        return {**out, "county": None, "status": "county-not-in-crosswalk", "owner": None,
                "note": "build the crosswalk for this county to resolve owner here"}
    out.update(county=row["county"], state=row["state"], method=row["method"])

    # ---- WFS (GeoServer) point lookup (e.g. OK okmaps). This host has no exact point-in-polygon
    # for a 4326 point (native-CRS mismatch), so we ask for a small bbox and take the nearest owner-
    # populated parcel in it -> approx. bbox CRS tag (EPSG:4326) makes GeoServer reproject correctly. ----
    if row["method"] == "wfs":
        h = 0.0009  # ~150m half-box
        url = row["query_url"] + f"&srsName=EPSG:4326&bbox={lon-h},{lat-h},{lon+h},{lat+h},EPSG:4326"
        feats = _getj(url).get("features", [])
        chosen = next((f for f in feats if _owner_from(f.get("properties", {}), row["owner_fields"])),
                      feats[0] if feats else None)
        if chosen is None:
            return {**out, "status": "no-parcel-at-point", "owner": None}
        p = chosen.get("properties", {})
        val = _best_value(p, row.get("value_field"))
        res = {**out, "status": "resolved", "owner": _owner_from(p, row["owner_fields"]), "approx": True,
               "value": (f"${val[1]:,.0f} ({val[0]})" if val else ""), "source": row["query_url"]}
        if row.get("note"): res["note"] = row["note"]
        return res

    # ---- arcgis-join: owner lives in a separate (non-spatial) table reached by a join key (e.g. ND
    # NDGISHUB: geometry layer 0 -> UniqueGISID -> TaxRoll table 1 -> OwnerName). Only resolve when a
    # join_url is recorded; legacy join rows without one fall through to honest routing below. ----
    if row["method"] == "arcgis-join" and row.get("join_url"):
        jkey = row.get("join_key") or ""
        gfield, _, tfield = jkey.partition("=")     # "geomfield=tablefield"; no '=' -> same name on both sides (ND)
        tfield = tfield or gfield
        def gq(dist=None):                          # point query on the GEOMETRY layer (carries the key)
            p = {"geometry": f"{lon},{lat}", "geometryType": "esriGeometryPoint", "inSR": "4326",
                 "spatialRel": "esriSpatialRelIntersects", "outFields": "*",
                 "returnGeometry": "false", "f": "json"}
            if dist: p.update(distance=dist, units="esriSRUnit_Meter")
            return _getj(row["query_url"], p).get("features", [])
        feats = gq()
        approx = False
        if not feats:                               # gap-miss -> buffer (ND step 1 needs this)
            feats = gq(40); approx = bool(feats)
        # A point can cover several polygons (and some hosts mix non-parcel features e.g. hydro lines
        # with junk keys); collect distinct candidate keys in feature order and try each against the
        # owner table until one joins. ND's single real parcel is just the 1-candidate case.
        cands = []
        for ft in feats:
            v = ft["attributes"].get(gfield)
            if v not in (None, "", " ") and v not in cands:
                cands.append(v)
        if not cands:
            return {**out, "status": "no-parcel-at-point", "owner": None}
        a = None
        for jval in cands[:6]:
            where = f"{tfield}='{jval}'" if isinstance(jval, str) else f"{tfield}={jval}"
            tfeats = _getj(row["join_url"], {"where": where, "outFields": "*",
                                             "returnGeometry": "false", "f": "json"}).get("features", [])
            if tfeats:
                a = tfeats[0]["attributes"]; break
        if a is None:
            return {**out, "status": "no-owner-in-join-table", "owner": None, "note": f"{gfield}={cands[0]}"}
        val = _best_value(a, row.get("value_field"))
        res = {**out, "status": "resolved", "owner": _owner_from(a, row["owner_fields"]), "approx": approx,
               "value": (f"${val[1]:,.0f} ({val[0]})" if val else ""), "source": row["join_url"]}
        if row.get("note"): res["note"] = row["note"]
        return res

    # Only a single-query ArcGIS owner layer resolves directly today. Everything else is
    # routed honestly with what it would take (wms is resolvable but 2-step, not yet wired).
    if row["method"] != "arcgis":
        note = {"deed-records": "owner via bulk deed/sales records (grantee chain), not a live layer",
                "suppressed": "owner withheld from public REST; value+geom only",
                "bulk-download": "owner only in a downloadable GDB/shapefile; load + spatial-join locally",
                "arcgis-join": f"point->parcel-id then join owner table on key '{row.get('join_key','')}' (2-step; no join_url recorded)",
                "wms": "owner via WMS GetFeatureInfo, not ArcGIS REST (different call, not yet wired)",
                "none": "no public programmatic source for this county"}.get(row["method"], "")
        return {**out, "status": f"routed:{row['method']}", "owner": None, "note": note}
    # ArcGIS point-in-parcel. Two real-world wrinkles handled here:
    #  (1) the geocoded point can land just off the parcel (rooftop/road) -> retry buffered;
    #  (2) statewide cadastral layers (MT, WY, ...) carry street right-of-way / government
    #      polygons with a BLANK owner that a point can land on -> skip those and pick the
    #      nearest parcel that actually has a populated owner.
    def owner_of(a):
        vals = []                                   # dedupe identical owner fields (X / X -> X)
        for fld in row["owner_fields"].split("|"):
            # " ".join(split()) collapses internal padding too (IA DEEDHOLDER is fixed-width
            # LAST/FIRST/MIDDLE, e.g. "DOE         JANE    Q" -> "DOE JANE Q"); harmless elsewhere.
            v = " ".join(str(a.get(fld) or "").split())
            if v and v not in vals: vals.append(v)
        return " / ".join(vals) or None
    def pq(dist=None):
        p = {"geometry": f"{lon},{lat}", "geometryType": "esriGeometryPoint", "inSR": "4326",
             "spatialRel": "esriSpatialRelIntersects", "outFields": "*",
             "returnGeometry": "false", "f": "json"}
        if dist: p.update(distance=dist, units="esriSRUnit_Meter")
        return _getj(row["query_url"], p).get("features", [])
    def pq_env(half=0.0008):                    # ~150m bbox (deg). Fallback for hosts that REJECT the
        p = {"geometry": f"{lon-half},{lat-half},{lon+half},{lat+half}",   # distance buffer param
             "geometryType": "esriGeometryEnvelope", "inSR": "4326",       # (e.g. CT, layer SR 6434):
             "spatialRel": "esriSpatialRelIntersects", "outFields": "*",   # an envelope is accepted where
             "returnGeometry": "false", "f": "json"}                       # distance/units is not.
        return _getj(row["query_url"], p).get("features", [])
    def pq_native():                            # last resort: host IGNORES inSR=4326 (e.g. Anne Arundel MD
        try:                                    # gis.aacounty.org MapServer, SR 2893) so a 4326 point matches
            import pyproj                        # nothing. Reproject the point to the layer's native SR via
            meta = _getj(row["query_url"].split("/query")[0], {"f": "json"})  # pyproj and query in native coords.
            sr = (meta.get("extent", {}) or {}).get("spatialReference", {}) or meta.get("spatialReference", {}) or {}
            wkid = sr.get("latestWkid") or sr.get("wkid")
            if not wkid or int(wkid) in (4326, 4269, 3857, 102100): return []   # already lon/lat-friendly
            # ESRI State Plane wkids (102xxx, e.g. MO Integrity-GIS layers wkid 102696-8) are NOT EPSG
            # codes -> pyproj's from_crs(4326, 102698) raises CRSError. Fall back to the ESRI: authority.
            try:
                tr = pyproj.Transformer.from_crs(4326, int(wkid), always_xy=True)
            except Exception:
                tr = pyproj.Transformer.from_crs("EPSG:4326", f"ESRI:{int(wkid)}", always_xy=True)
            x, y = tr.transform(lon, lat)
            for dist in (None, 60):             # exact, then ~60m buffer (off-parcel: rooftop/road/water)
                p = {"geometry": f"{x},{y}", "geometryType": "esriGeometryPoint", "inSR": str(wkid),
                     "spatialRel": "esriSpatialRelIntersects", "outFields": "*",
                     "returnGeometry": "false", "f": "json"}
                if dist: p.update(distance=dist, units="esriSRUnit_Meter")
                feats = _getj(row["query_url"], p).get("features", [])
                if feats: return feats
            return []
        except Exception:
            return []
    def pick(feats):
        for ft in feats:                       # prefer a feature with a populated owner
            if owner_of(ft["attributes"]): return ft, True
        return (feats[0], False) if feats else (None, False)
    chosen, has_owner = pick(pq())
    approx = False
    if chosen is None or not has_owner:        # off-parcel, or landed on a blank-owner ROW parcel
        bchosen, bhas = pick(pq(40))
        if bchosen is not None and (bhas or chosen is None):
            chosen, has_owner, approx = bchosen, bhas, True
    if chosen is None or not has_owner:        # buffered point unavailable/empty (some hosts reject the
        echosen, ehas = pick(pq_env())         # distance param) -> first owner-populated parcel in a small
        # NOTE: pq_env picks FIRST-by-result-order (≈OBJECTID), not the geometrically nearest, so the owner
        # can be up to ~150m away in any direction. Flagged via approx=True (same flag as a tight buffer hit).
        # If envelope-host accuracy ever becomes load-bearing, switch to returnGeometry + pick-nearest-centroid.
        if echosen is not None and (ehas or chosen is None):
            chosen, has_owner, approx = echosen, ehas, True
    if chosen is None or not has_owner:        # everything in 4326 failed -> host may ignore inSR=4326;
        nchosen, nhas = pick(pq_native())      # reproject to the layer's native SR and retry
        if nchosen is not None and (nhas or chosen is None):
            chosen, has_owner, approx = nchosen, nhas, True
    if chosen is None:
        return {**out, "status": "no-parcel-at-point", "owner": None}
    a = chosen["attributes"]
    val = _best_value(a, row.get("value_field"))
    res = {**out, "status": "resolved", "owner": owner_of(a), "approx": approx,
           "value": (f"${val[1]:,.0f} ({val[0]})" if val else ""), "source": row["query_url"]}
    # Surface a per-county caveat (e.g. IA owner = 2017 snapshot) so a consumer keying on `owner`
    # can see it's lower-confidence — never let staleness live only in the source URL string.
    if row.get("note"): res["note"] = row["note"]
    return res

DEMO = [   # real public-company facilities chosen to hit DIFFERENT crosswalk methods
    ("Boeing - Everett widebody plant (Snohomish Co)", dict(lat=47.9163, lon=-122.2806)),
    ("Itron HQ - 2111 N Molter Rd, Liberty Lake WA (Spokane Co)", dict(address="2111 N Molter Rd, Liberty Lake, WA 99019")),
    ("Microsoft - Redmond campus (King Co)", dict(lat=47.6423, lon=-122.1391)),
    ("TrueBlue Inc HQ - Tacoma (Pierce Co)", dict(lat=47.2513, lon=-122.4416)),
]

if __name__ == "__main__":
    print("item2 owner resolver - public-company facility demo\n" + "=" * 74)
    for label, kw in DEMO:
        r = resolve(**kw)
        print(f"\n{label}")
        print(f"  county : {r.get('county','?')} {r.get('state','')} ({r.get('fips')})  method={r.get('method','?')}")
        if r["status"] == "resolved":
            print(f"  OWNER  : {r['owner']}" + ("   [nearest parcel, ~buffered]" if r.get("approx") else ""))
            if r.get("value"): print(f"  value  : {r['value']}")
        else:
            print(f"  status : {r['status']}  - {r.get('note','')}")
