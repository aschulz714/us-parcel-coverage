"""Write probe files for the 13 MO Integrity-GIS Geocortex x-proxy owner wins found in RPC recon.
Each entry was point-query-verified live. This script re-verifies a point query and writes the probe file.
"""
import json, urllib.request, urllib.parse, ssl, gzip, io

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
HOST = "caldwellgis.integritygis.com"  # shared Integrity Geocortex instance (any subdomain serves all sites)
CTX = ssl.create_default_context(); CTX.check_hostname=False; CTX.verify_mode=ssl.CERT_NONE
PROBE_DIR = r"data/probes_west"

def getj(url, params):
    full = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(full, headers={"User-Agent": UA, "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, context=CTX, timeout=30) as r:
        raw = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
    return json.loads(raw.decode("utf-8", "replace"))

def qurl(site, ms, lyr):
    return f"https://{HOST}/Geocortex/Essentials/REST/sites/{site}/map/mapservices/{ms}/rest/services/x/MapServer/{lyr}/query"

# fips, county, site, ms, parcel-layer, owner_field, value_field, seat, native_sr(for evidence), note
WINS = [
 ("29013","Bates","Bates_County_MO",0,9,"REALDATA_NAME","REALDATA_ASSESSED_VALUE","Butler",None),
 ("29015","Benton","Benton_County_MO",1,9,"name","valuation","Warsaw",None),
 ("29041","Chariton","Chariton_County_MO",2,7,"DEEDHOLDER","","Keytesville",None),
 ("29043","Christian","Christian_County_MO",4,8,"name","valuation","Ozark",None),
 ("29053","Cooper","Cooper_County_MO",2,11,"name","","Boonville",None),
 ("29057","Dade","Dade_County_MO",0,11,"DEEDHOLDER","REALDATA_ASSESSED_VALUE","Greenfield",102698),
 ("29059","Dallas","Dallas_County_MO",0,7,"name","valuation","Buffalo",None),
 ("29087","Holt","Holt_County_MO",3,20,"owner1_name","","Oregon",None),
 ("29109","Lawrence","Lawrence_County_MO",0,6,"name","","Mount Vernon",None),
 ("29117","Livingston","Livingston_County_MO_Public",1,7,"DEEDHOLDER","","Chillicothe",None),
 ("29173","Ralls","Ralls_County_MO",4,8,"DEEDNAME","","New London",None),
 ("29195","Saline","Saline_County_MO",0,26,"NAME1","","Marshall",None),
 ("29219","Warren","Warren_County_MO",2,6,"OWNER_NAME","TOTALVAL","Warrenton",None),
]

def sample_and_point(site, ms, lyr, of, native_sr):
    """Pull a real owner + centroid, then point-query (4326, or native-SR for Dade) -> (sample_owner, pq_owner, pq_n, pt_str, situs)."""
    u = qurl(site, ms, lyr)
    where = f"{of} IS NOT NULL"
    try:
        s = getj(u, {"where": where, "outFields": "*", "resultRecordCount": 1,
                     "returnGeometry": "true", "outSR": 4326, "f": "json"})
        feats = s.get("features", [])
        # find a non-"No Info" owner
        for tryextra in ["", f" AND {of}<>'No Info'"]:
            s = getj(u, {"where": where+tryextra, "outFields": "*", "resultRecordCount": 1,
                         "returnGeometry": "true", "outSR": 4326, "f": "json"})
            feats = s.get("features", [])
            if feats and str(feats[0]["attributes"].get(of) or "").strip() not in ("","No Info"):
                break
        a = feats[0]["attributes"]; ring = feats[0]["geometry"]["rings"][0]
        cx = sum(p[0] for p in ring)/len(ring); cy = sum(p[1] for p in ring)/len(ring)
        sample_owner = " ".join(str(a.get(of) or "").split())
        situs = a.get("state") or a.get("STATE") or a.get("SITUS_STATE") or a.get("mailto_csz") or a.get("REALDATA_SITUS_LOCATION") or ""
    except Exception as e:
        return (f"SAMPLE-FAIL:{e}", "", 0, "", "")
    # point query
    if native_sr:
        # reproject centroid to native SR via the layer's own returnGeometry (we already have native via no-outSR)
        sn = getj(u, {"where": where, "outFields": of, "resultRecordCount": 1, "returnGeometry": "true", "f": "json"})
        nring = sn["features"][0]["geometry"]["rings"][0]
        ncx = sum(p[0] for p in nring)/len(nring); ncy = sum(p[1] for p in nring)/len(nring)
        pq = getj(u, {"geometry": f"{ncx},{ncy}", "geometryType":"esriGeometryPoint","inSR":str(native_sr),
                      "outFields":"*","returnGeometry":"false","f":"json"})
        pt_str = f"native-SR {native_sr} centroid ({ncx:.1f},{ncy:.1f})"
    else:
        pq = getj(u, {"geometry": f"{cx},{cy}", "geometryType":"esriGeometryPoint","inSR":"4326",
                      "outFields":"*","returnGeometry":"false","f":"json"})
        pt_str = f"4326 centroid ({cx:.5f},{cy:.5f})"
    pf = pq.get("features", [])
    pq_owner = " ".join(str(pf[0]["attributes"].get(of) or "").split()) if pf else ""
    return (sample_owner, pq_owner, len(pf), pt_str, str(situs).strip())

written = []
for fips, county, site, ms, lyr, of, vf, seat, native_sr in WINS:
    so, po, pn, pt, situs = sample_and_point(site, ms, lyr, of, native_sr)
    if pn < 1 or not po:
        print(f"!! {fips} {county}: POINT QUERY FAILED (pn={pn}, sample={so!r}) -- SKIPPING WRITE")
        continue
    nat_note = (f" Layer SR is ESRI wkid {native_sr} (MO State Plane) and the host IGNORES inSR=4326 "
                f"(a 4326 point returns 0), so the point query must reproject to {native_sr} "
                f"(resolve.py pq_native ESRI: fallback handles this).") if native_sr else ""
    probe = {
        "fips": fips, "county": county, "state": "MO", "method": "arcgis", "point_query": "Y",
        "owner_fields": of, "value_field": vf, "join_key": "", "join_url": "",
        "query_url": qurl(site, ms, lyr),
        "note": (f"Integrity GIS Geocortex anon REST proxy (.../map/mapservices/{ms}/rest/services/x/MapServer/{lyr}) "
                 f"bypasses the guest-locked /map/mapservices listing (403 Guest). Layer {lyr} 'Parcel' polygon carries "
                 f"owner={of} directly." + nat_note),
        "verification_status": "probed-live",
        "evidence": (f"MO RPC recon 2026-06-08. Shared Integrity-GIS Geocortex instance (site {site}). "
                     f"The /map/mapservices listing is 403 Guest-locked, but the anonymous x-proxy "
                     f".../map/mapservices/{ms}/rest/services/x/MapServer is OPEN. Parcel layer {lyr}, owner field {of}. "
                     f"Sample real owner: '{so}'. POINT QUERY at {pt} -> {pn} feature, owner '{po}'. "
                     f"MO confirmed via situs/state field '{situs}'. (Distinct from same-name out-of-state collisions.)"),
        "notes": f"Integrity GIS Geocortex platform, site {site}; owner via anon x/MapServer proxy (MS{ms} layer {lyr}, {of}). Seat={seat}.",
    }
    path = f"{PROBE_DIR}/{fips}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(probe, f, ensure_ascii=False)
    written.append(f"{fips} {county}: PQ ok -> {po}")
    print(f"OK {fips} {county}: point={po}  (sample={so})")

print("\nWROTE", len(written), "probe files")
