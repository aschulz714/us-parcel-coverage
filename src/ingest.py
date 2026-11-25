"""Bulk county parcel extractor — the DATASET side of the project.

Where resolve.py answers one point at a time against live county servers, this walks the
crosswalk (county_methods.csv) and downloads each county's FULL parcel layer (geometry +
all attributes) into one GeoParquet per county under data/parcels/raw/. That local store is
what makes research queries possible (owner search across counties, value filters, mapping)
and is the first stage of the Regrid-style pipeline:

    ingest.py (raw pull) -> normalize.py (standard schema) -> tiles/research queries

Extractable crosswalk methods: `arcgis` (2,195+ counties nationally) and `arcgis-join` with a
recorded join_url (geometry layer + owner TABLE merged locally on the join key — the ME/ND/OK
class). `wfs` still needs its own extractor.

Usage:
  python src/ingest.py --state WA               # every arcgis county in a state
  python src/ingest.py --fips 53069 53055       # specific counties
  python src/ingest.py --state WA --limit 2     # smoke test: first N counties
  python src/ingest.py --state WA --refresh     # re-pull even if already downloaded

Each successful county updates data/parcels/manifest.json with feature counts, retrieval
time, and the layer's lastEditDate (the currency signal for refresh scheduling).
"""
import argparse, csv, datetime, io, json, os, re, subprocess, sys, time
from urllib.parse import urlencode

import geopandas as gpd
import pandas as pd
import requests

from atomic_io import atomic_replace_writer
from ingest_integrity import ReconciliationError, reconcile_feature_counts, write_ingest_audit

HERE = os.path.dirname(__file__)
ROOT = os.path.dirname(HERE)
CROSSWALK = os.path.join(HERE, "county_methods.csv")
OUT_DIR = os.path.join(ROOT, "data", "parcels")

# Same browser-like UA as resolve.py (some hosts sit behind WAFs that block non-browser UAs).
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"}

STATE_BY_FIPS = {}  # "53" -> "WA", built from the crosswalk's state column

# Florida DOR county numbers (the statewide cadastral layer's CO_NO), keyed by county name
# as it appears in the crosswalk. Standard DOR coding: alphabetical by HISTORICAL name —
# Miami-Dade is 'Dade' (23). Spot-verified empirically before the FL run (centroid-in-county).
FL_DOR_BY_NAME = {
    "Alachua": 11, "Baker": 12, "Bay": 13, "Bradford": 14, "Brevard": 15, "Broward": 16,
    "Calhoun": 17, "Charlotte": 18, "Citrus": 19, "Clay": 20, "Collier": 21, "Columbia": 22,
    "Miami-Dade": 23, "DeSoto": 24, "Dixie": 25, "Duval": 26, "Escambia": 27, "Flagler": 28,
    "Franklin": 29, "Gadsden": 30, "Gilchrist": 31, "Glades": 32, "Gulf": 33, "Hamilton": 34,
    "Hardee": 35, "Hendry": 36, "Hernando": 37, "Highlands": 38, "Hillsborough": 39,
    "Holmes": 40, "Indian River": 41, "Jackson": 42, "Jefferson": 43, "Lafayette": 44,
    "Lake": 45, "Lee": 46, "Leon": 47, "Levy": 48, "Liberty": 49, "Madison": 50,
    "Manatee": 51, "Marion": 52, "Martin": 53, "Monroe": 54, "Nassau": 55, "Okaloosa": 56,
    "Okeechobee": 57, "Orange": 58, "Osceola": 59, "Palm Beach": 60, "Pasco": 61,
    "Pinellas": 62, "Polk": 63, "Putnam": 64, "St. Johns": 65, "St. Lucie": 66,
    "Santa Rosa": 67, "Sarasota": 68, "Seminole": 69, "Sumter": 70, "Suwannee": 71,
    "Taylor": 72, "Union": 73, "Volusia": 74, "Wakulla": 75, "Walton": 76, "Washington": 77,
}

# Wide statewide layers can be pulled with a TRIMMED outFields set — Florida's full NAL
# schema is ~120 columns and made full-attribute pages ~5-10x heavier than needed.
STATEWIDE_OUTFIELDS = {
    "Florida_Statewide_Cadastral": (
        "CO_NO,PARCEL_ID,OWN_NAME,OWN_ADDR1,OWN_ADDR2,OWN_CITY,OWN_STATE,OWN_ZIPCD,"
        "JV,AV_SD,LND_VAL,DOR_UC,PHY_ADDR1,PHY_ADDR2,PHY_CITY,PHY_ZIPCD,"
        "ASMNT_YR,LND_SQFOOT,SALE_PRC1,SALE_YR1,ACT_YR_BLT,TOT_LVG_AR"),
}

# Per-layer page-size override (url substring -> rows/page), for layers whose host is slow at the
# default 2000-row cap but advertises a far larger maxRecordCount. NY's statewide layer serves a
# 2000-row geometry page in ~12s (Erie 370k -> ~36min) but supports 50000; 10000 cuts it ~3x while
# staying well under page-timeout/size risk. Verified 2026-06-20.
STATEWIDE_PAGESIZE = {
    "NYS_Tax_Parcels_Public": 10000,
    "plan_parcels_open": 10000,  # MN statewide advertises maxRecordCount 20000; 2000 is needlessly slow
}

# Alaska AK_Parcels statewide aggregation keys parcels by `local_gov` NAME (no fips field), so
# map each contributing government to its borough/census-area fips. 18 of AK's organized govs are
# on this layer (Juneau/Lake&Pen/Skagway self-host; 8 census areas have no owner source). Several
# census areas appear only as their seat CITY (Unalaska->Aleutians West, Valdez->Valdez-Cordova,
# Nome, Dillingham) -> partial coverage, flagged. Verified 2026-06-19 (groupBy local_gov).
AK_LOCALGOV_BY_FIPS = {
    "02016": "City of Unalaska", "02020": "Anchorage Municipality",
    "02060": "Bristol Bay Borough", "02068": "Denali Borough", "02070": "City of Dillingham",
    "02090": "Fairbanks North Star Borough", "02100": "Haines Borough",
    "02122": "Kenai Peninsula Borough", "02130": "Ketchikan Borough",
    "02150": "Kodiak Island Borough", "02170": "Matanuska-Susitna Borough",
    "02180": "City of Nome", "02185": "North Slope Borough", "02195": "Petersburg Borough",
    "02220": "Sitka City & Borough", "02261": "City of Valdez",
    "02275": "Wrangell City & Borough", "02282": "Yakutat City & Borough",
}

# Shared STATEWIDE layers: one endpoint serves every county, so per-county pulls need a
# synthesized where-slice (url substring -> where builder from the crosswalk row).
# ATCi x911Parcels shared FeatureServer (services9.arcgis.com/zDY8zW24CdJKhVIw) serves several KS
# counties from one layer, keyed by a LAYER-INTERNAL CountyCode (NOT the fips). The crosswalk notes
# recorded the per-county code but the slicer was never added, so Ness/Norton each pulled the full
# 389k-row layer (confirmed dup, 2026-06-29). Codes spatially confirmed by the crosswalk author.
X911_COUNTYCODE = {"20135": "068", "20137": "069",  # Ness, Norton
                   # KS rural wave 2026-07-17 (recon/ks_rural_recon.md); codes are the
                   # layer's literal values — zero-padding is INCONSISTENT at source
                   "20023": "012", "20069": "35", "20081": "041", "20093": "047",
                   "20163": "082", "20165": "83", "20181": "091", "20203": "102"}

# Iowa_Parcels_2017 COUNTYNAME values that are not simply the upper-cased county name. The layer
# spells O'Brien with a space ("O' BRIEN"): 'O''BRIEN' matches 0 rows, the spaced form 18,267
# (= our pull; verified 2026-09-28).
IA_COUNTYNAME = {"O'Brien": "O' BRIEN"}

MD_JURSCODE = {"24001": "ALLE", "24009": "CALV", "24013": "CARR", "24021": "FRED",
               "24023": "GARR", "24027": "HOWA", "24037": "STMA", "24041": "TALB",
               "24047": "WORC"}  # MD rural-tail fill 2026-07-17 (statewide SDAT layer)

STATEWIDE_SLICERS = [
    # IndianaMap statewide parcel fabric — all 92 IN counties (sweep 2026-07-18)
    ("gisdata.in.gov/server/rest/services/Hosted/Parcel_Boundaries_of_Indiana_Current",
     lambda row: "county_fips='{}'".format(row["fips"])),
    # VGIN statewide VA parcels — all localities (sweep 2026-07-18); Culpeper merges
    # its town-FIPS rows
    ("vginmaps.vdem.virginia.gov/arcgis/rest/services/VA_Base_Layers/VA_Parcels",
     lambda row: ("FIPS IN ('51047','5120752')" if row["fips"] == "51047"
                  else "FIPS='{}'".format(row["fips"]))),
    # MD iMAP statewide ParcelBoundaries (mdgeodata.md.gov) serves all 24 MD counties;
    # JURSCODE = SDAT county code. Values NFMTTLVL + landuse + situs; owner NAME absent
    # (mailing only) — the source's suppression, not ours.
    ("mdgeodata.md.gov/imap/rest/services/PlanningCadastre/MD_ParcelBoundaries",
     lambda row: "JURSCODE='{}'".format(MD_JURSCODE[row["fips"]])),
    # x911 rows are ~4x stacked with only one owner-populated copy per parcel — the
    # Owner filter is the dedup (verified per-county 2026-07-17: Ness 23,928 -> 5,979)
    ("zDY8zW24CdJKhVIw/arcgis/rest/services/x911Parcels",
     lambda row: "CountyCode='{}' AND Owner NOT IN ('0',' ','')".format(
         X911_COUNTYCODE[row["fips"]])),
    # NE OCIO statewide layer serves all 93 NE counties (rural-tail backfill 2026-07-17);
    # County_ID = the 3-digit county code, i.e. the fips without its '31' state prefix.
    ("gis.ne.gov/Enterprise/rest/services/StatewideParcelsExternal",
     lambda row: "County_ID='{}'".format(row["fips"][2:])),
    # DANC (Development Authority of the North Country) Parcel_Model serves ~10 NY north-country
    # counties from one layer; only Clinton + Jefferson use it here. COUNTY field has inconsistent
    # casing ('CLINTON', 'Jefferson') and a 'JEFFERSON - Watertown' enclave split, so match by
    # case-insensitive prefix on the county name. (Confirmed dup 2026-06-29: identical 366,700 apns.)
    ("maps.dancgis.org/server/rest/services/Parcel_Model",
     lambda row: "UPPER(COUNTY) LIKE '{}%'".format(row["county"].upper().replace("'", "''"))),
    # districtiii.org TRIPP_TODD_WEB_LAYERS is a shared Tripp+Todd SD layer; slice on TRIPP_TODD_COUNTY
    # ('TODD'/'TRIPP'). (Confirmed dup 2026-06-29: both pulled the full 15,513-row 2-county layer.)
    ("gis.districtiii.org/server/rest/services/TRIPP_TODD",
     lambda row: "TRIPP_TODD_COUNTY='{}'".format(row["county"].upper().replace("'", "''"))),
    # Hampton Roads HRSD regional layer (geo.hrsd.com) serves 11 VA localities from ONE layer; without
    # a slice every locality pulled the full ~650k-parcel region (confirmed dup: Newport News/Norfolk/
    # Portsmouth had identical apn sets, 2026-06-29). FIPS field = the 3-digit VA locality code (51700
    # -> '700', 51073 -> '073'), zero-padded. Verified against the layer's distinct FIPS values.
    ("geo.hrsd.com/hrgeo/rest/services/regionalgis/HRGeo_Parcels_Public",
     lambda row: "FIPS='{}'".format(row["fips"][2:])),
    ("idl.idaho.gov/arcgis/rest/services/Portal/WhiteStar_Parcels",
     lambda row: f"fips='{row['fips']}'"),
    # Montana Cadastral: gisservicemt.gov/.../MSDI_Framework/Parcels died 2026-09 (host serves an
    # HTML page); successor msdi_cadastral_map_v1/MapServer/1 has the same schema incl. CountyName
    # (all 56 counties' counts + field_map fields re-verified 2026-09-28).
    ("gisservice.mt.gov/arcgis/rest/services/msdi_cadastral_map_v1",
     lambda row: "UPPER(CountyName)='{}'".format(row["county"].upper().replace("'", "''"))),
    ("Tennessee_Property_Boundaries",
     lambda row: "UPPER(COUNTY_NAME)='{}'".format(row["county"].upper().replace("'", "''"))),
    ("Florida_Statewide_Cadastral",
     lambda row: f"CO_NO={FL_DOR_BY_NAME[row['county']]}"),
    ("nconemap.gov/secure/rest/services/NC1Map_Parcels",
     lambda row: f"cntyfips='{row['fips'][2:]}'"),
    ("Wisconsin_Statewide_Parcels",
     lambda row: f"PARCELFIPS='{row['fips'][2:]}'"),
    ("geostor.arkansas.gov/arcgis/rest/services/FEATURESERVICES/Planning_Cadastre",
     lambda row: f"countyfips='{row['fips']}'"),
    # WV CountyID = 1 + index of the county in plain-sorted name order (derived EMPIRICALLY
    # via point-in-county on sampled parcels, 2026-06-10; border-parcel samples confirmed
    # adjacent-county slips only — the sorted-order pattern held for every clean sample)
    ("wvgis.wvu.edu/arcgis/rest/services/Planning_Cadastre/WV_Parcels",
     lambda row: f"CountyID='{_wv_county_id(row)}'"),
    ("Wyoming_Parcels",  # layer strips spaces from county names: HOTSPRINGS, BIGHORN
     lambda row: "jurisdicti='{}'".format(row["county"].upper().replace(" ", "").replace("'", "''"))),
    # MEGIS organized-towns statewide layer + its ADB owner TABLE (arcgis-join): the table has no
    # COUNTY/CNTYCODE column, but BOTH sides carry the 5-char town GEOCODE whose first 2 chars are
    # the county code == last 2 digits of the county fips (23001->'01'; verified via distinct
    # COUNTY/CNTYCODE pairs 2026-07-02). A GEOCODE prefix is the one where that slices both.
    ("Maine_Parcels_Organized_Towns",
     lambda row: f"GEOCODE LIKE '{row['fips'][3:]}%'"),
    # One joined layer serves Maui county AND Kalawao (TMK zone 2-6-1 = the 10 Kalaupapa-peninsula
    # parcels, verified count 2026-07-03) — slice so the two fips don't double-ingest.
    ("Maui_County_Parcels_Owners_Joined",
     lambda row: "tmk_txt LIKE '261%'" if row["fips"] == "15005" else "tmk_txt NOT LIKE '261%'"),
    # Ohio ODNR odnr_landbase serves 81 counties (the 7 owner-stripped metros self-host
    # separately). COUNTY is proper-case incl 'Van Wert' (verified 2026-06-12, recon/oh_preflight.md).
    ("odnr_landbase",
     lambda row: "COUNTY='{}'".format(row["county"].replace("'", "''"))),
    # Connecticut statewide CAMA layer: FIPS field is the parcel's old-county fips prefix; slice
    # by LIKE on the 8 old-county fips (planning-region 091xx fips aren't in the layer -- ingest
    # only the 8 old counties). Verified 2026-06-12, recon/ct_preflight.md.
    ("Connecticut_CAMA_and_Parcel_Layer",
     lambda row: "FIPS LIKE '{}%'".format(row["fips"])),
    # New Hampshire NHGRANIT CAD_ParcelMosaicSecure: slice by CountyId (string '1'..'10',
    # alphabetical county order == NH fips ascending, so (fips_county_num+1)/2). Verified
    # 2026-06-12, recon/nh_preflight.md. (CountyId-NULL fabric rows are not in any slice.)
    ("CAD_ParcelMosaicSecure",
     lambda row: "CountyId='{}'".format((int(row["fips"][2:]) + 1) // 2)),
    # Colorado statewide composite (gis.colorado.gov Colorado_Public_Parcels): slice on the
    # countyName text field (title-case, e.g. 'Denver','El Paso'). 41 of CO's 64 counties on this
    # layer; the other big counties (El Paso, Delta, Fremont, Montrose, ...) have own arcgis rows.
    # Verified 2026-06-17 (recon/western_states.md): countyName='Denver' -> 240,257.
    ("Colorado_Public_Parcels",
     lambda row: "countyName='{}'".format(row["county"].replace("'", "''"))),
    # Mississippi MARIS statewide (gis.mississippi.edu MS_Parcels_August_2024): two polygon
    # layers (1=West, 2=East), each county's crosswalk row carries the correct layer URL; slice
    # by full 5-digit STCNTYFIPS. Layer has supportsPagination=false -> objectIds path. Verified
    # 2026-06-19 (recon/expansion_recon_2026-06-19.md): STCNTYFIPS='28001' -> 18,558 (Adams).
    ("MS_Parcels_August_2024",
     lambda row: "STCNTYFIPS='{}'".format(row["fips"])),
    # North Dakota NDGISHUB_Parcels: split design (layer 0 polygons + table 1 taxroll joined on
    # UniqueGISID). BOTH layers carry CountyFIPS, so the same slice filters the geometry pull and
    # the owner-table pull (see _join_table, which passes this where to _fetch_attr_table). Verified
    # 2026-06-19: CountyFIPS='38001' -> 5,185 geom / 7,005 table rows.
    ("NDGISHUB_Parcels",
     lambda row: "CountyFIPS='{}'".format(row["fips"])),
    # Alaska statewide DNR aggregation (AK_Parcels): no fips field -> slice by local_gov NAME via
    # the fips->name map above. Only the 18 statewide-layer rows carry this URL (own-host boroughs
    # have their own endpoints). Verified 2026-06-19.
    ("AK_Parcels/FeatureServer",
     lambda row: "local_gov='{}'".format(AK_LOCALGOV_BY_FIPS[row["fips"]].replace("'", "''"))),
    # New York NYS_Tax_Parcels_Public statewide layer (38 of 62 counties authorize redistribution).
    # COUNTY_NAME is the county name with spaces/periods stripped: 'NewYork','StLawrence'. The 38
    # covered counties' crosswalk rows point here (owner=PRIMARY_OWNER, value=FULL_MARKET_VAL —
    # equalized market value, more comparable across NY's fractional-assessment towns than TOTAL_AV;
    # 99.8% populated in Albany); the other 24 keep their per-county rows. Verified 2026-06-19.
    ("NYS_Tax_Parcels_Public",
     lambda row: "COUNTY_NAME='{}'".format(row["county"].replace(" County", "").replace(".", "").replace(" ", ""))),
    # Iowa statewide Iowa_Parcels_2017 (services3.arcgis.com): COUNTYNAME is UPPERCASE (ADAIR).
    # 74 of 99 IA counties ride this 2017 layer; the other 25 have current per-county rows.
    # Verified 2026-06-20: 99 distinct COUNTYNAME values.
    ("Iowa_Parcels_2017",
     lambda row: "COUNTYNAME='{}'".format(
         IA_COUNTYNAME.get(row["county"], row["county"].upper()).replace("'", "''"))),
    # Minnesota statewide plan_parcels_open (MnGeo, via utility.arcgis.com proxy): co_name is
    # title-case (Aitkin). 36 of 80 MN counties on this opt-in layer (59 statewide); 44 per-county.
    # Verified 2026-06-20: 59 distinct co_name values.
    ("plan_parcels_open",
     lambda row: "co_name='{}'".format(row["county"].replace("'", "''"))),
    # Massachusetts MassGIS L3 statewide: no county field, keys on TOWN_ID (1-351). Slice each of
    # the 14 counties by its set of TOWN_IDs, derived by point-in-county (data/ma_town_county.csv,
    # recon/derive_town_county.py). owner=OWNER1 (SDE IS NOT NULL quirk handled in normalize).
    ("L3Parcels_feature_service",
     lambda row: "TOWN_ID IN ({})".format(",".join(_towns_for("MA", row["fips"])) or "-1")),
    # Vermont VCGI statewide: no county field, keys on TOWN name. Slice by the county's town set
    # (data/vt_town_county.csv, point-in-county derived). TOWN is uppercase and never null.
    ("FS_VCGI_VTPARCELS",
     lambda row: "TOWN IN ({})".format(",".join("'" + t.replace("'", "''") + "'"
                                                 for t in _towns_for("VT", row["fips"])) or "''")),
]

_WV_IDS = {}


def _wv_county_id(row):
    if not _WV_IDS:
        with open(CROSSWALK, encoding="utf-8") as f:
            names = sorted(r["county"] for r in csv.DictReader(f) if r["state"] == "WV")
        _WV_IDS.update({n: f"{i+1:02d}" for i, n in enumerate(names)})
    return _WV_IDS[row["county"]]


_TOWN_COUNTY = {}  # st -> {county_fips: [town identifiers]}


def _towns_for(st, fips):
    """Town identifiers in a county, from data/<st>_town_county.csv (point-in-county derived by
    recon/derive_town_county.py) — for town-based statewide layers with no county field (MA/VT)."""
    if st not in _TOWN_COUNTY:
        path = os.path.join(ROOT, "data", f"{st.lower()}_town_county.csv")
        d = {}
        with open(path, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                d.setdefault(r["county_fips"], []).append(str(r["town"]))
        _TOWN_COUNTY[st] = d
    return _TOWN_COUNTY[st].get(fips, [])


def _statewide_where(row):
    for sub, build in STATEWIDE_SLICERS:
        if sub in (row.get("query_url") or ""):
            return build(row)
    return None


def _statewide_outfields(row):
    for sub, fields in STATEWIDE_OUTFIELDS.items():
        if sub in (row.get("query_url") or ""):
            return fields
    return "*"


def _getj(sess, url, params, *, tries=3, timeout=180, post=False):
    """GET/POST -> parsed JSON with retry + the resolve.py TLS fallback (verify=False only
    after an SSLError — some county hosts serve broken chains; data is public read-only).
    POST is used for objectIds batches: hosts like Mason WA 404 long GET URLs."""
    send = (lambda **kw: sess.post(url, data=params, headers=UA, timeout=timeout, **kw)) if post \
        else (lambda **kw: sess.get(url, params=params, headers=UA, timeout=timeout, **kw))
    last = None
    for attempt in range(tries):
        try:
            r = send()
            r.raise_for_status()
            return r.json(), r.content
        except requests.exceptions.SSLError as e:
            last = e
            try:
                r = send(verify=False)
                r.raise_for_status()
                return r.json(), r.content
            except Exception as e2:
                last = e2
        except Exception as e:
            last = e
        time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"GET failed after {tries} tries: {url} :: {last}")


def _page_to_gdf(content, fmt):
    """Parse one response page (GeoJSON or Esri JSON bytes) into a GeoDataFrame.
    pyogrio/GDAL autodetects both drivers, including dotted SDE field names."""
    gdf = gpd.read_file(io.BytesIO(content))
    if gdf.crs is None:
        gdf = gdf.set_crs(4326)
    return gdf


def _fetch_pages(sess, query_url, base_params, page, fmt, count_hint, label):
    """Pull every page via resultOffset pagination until an empty page comes back.
    Looping to the empty page (rather than len<page) survives servers that silently
    cap responses below the requested resultRecordCount."""
    pages, offset = [], 0
    while True:
        params = dict(base_params, f=fmt, resultOffset=offset, resultRecordCount=page)
        d, content = _getj(sess, query_url, params)
        if isinstance(d, dict) and d.get("error"):
            raise RuntimeError(f"server error: {d['error'].get('message')}")
        feats = d.get("features", [])
        if offset == 0 and count_hint and count_hint > 1000 and 0 < len(feats) < 10:
            # flaky-host insurance (Columbia OR intermittently returns 1 of 28,807): one
            # cool-down retry of the first page before trusting a near-empty response
            time.sleep(8)
            d, content = _getj(sess, query_url, params)
            feats = d.get("features", [])
        if not feats:
            break
        pages.append(_page_to_gdf(content, fmt))
        offset += len(feats)
        print(f"    {label}: {offset}{'/' + str(count_hint) if count_hint else ''} features", flush=True)
        if count_hint and offset >= count_hint:
            break
        time.sleep(0.2)  # politeness — these are county servers, not CDNs
    return pages


def _curl_get_json(query_url, params, timeout=40):
    """GET via curl --max-time (hard wall-clock kill). Bounds servers that dribble bytes and
    defeat requests' read-timeout — e.g. the objectIds returnIdsOnly preamble that hung MO Iron /
    IN Elkhart / SC Beaufort. Raises RuntimeError on timeout/transport/parse error so the caller
    FAILs the county fast instead of stalling the whole ingest."""
    q = query_url if query_url.rstrip("/").endswith("query") else query_url.rstrip("/") + "/query"
    url = q + "?" + urlencode(params)
    try:
        r = subprocess.run(["curl", "-sk", "--max-time", str(timeout), url],
                           capture_output=True, text=True, timeout=timeout + 5)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"curl hard-timeout after {timeout}s (server hang): {q}")
    out = (r.stdout or "").strip()
    if not out:
        raise RuntimeError(f"curl empty response (server hang/down): {q}")
    try:
        d = json.loads(out)
    except ValueError:
        raise RuntimeError(f"curl non-json response: {q}")
    if isinstance(d, dict) and d.get("error"):
        raise RuntimeError("server error: " + str(d["error"].get("message", "")))
    return d


class _ServerHang(Exception):
    """Raised when an objectIds chunk times out on every attempt with no error response —
    the server is hanging, not returning poison. Carries chunk size for skip-counting."""
    def __init__(self, ids):
        self.ids = list(ids)
        self.n = len(self.ids)
        super().__init__(f"server hang on {self.n}-record chunk")


def _fetch_by_objectids(sess, query_url, base_params, meta, fmt, label, check_field=None):
    """Fallback for servers without resultOffset pagination (the San Juan WA class):
    pull all object IDs, then query them in batches. check_field (the county's owner
    column) gates the per-batch retry — static columns stay populated even when a flaky
    SDE join nulls the fields that matter."""
    d = _curl_get_json(query_url, {"where": base_params.get("where", "1=1"),
                                   "returnIdsOnly": "true", "f": "json"}, timeout=40)
    oid_field = d.get("objectIdFieldName") or meta.get("objectIdField") or "OBJECTID"
    ids = d.get("objectIds") or []
    if not ids:
        raise RuntimeError("returnIdsOnly returned no ids")
    batch = max(1, min(500, int(meta.get("maxRecordCount") or 500)))
    pages = []
    skipped_ids = []
    consec_hang = 0
    for i in range(0, len(ids), batch):
        chunk = ids[i:i + batch]
        try:
            chunk_pages, chunk_skipped = _fetch_oid_chunk(
                sess, query_url, base_params, chunk, fmt, check_field)
        except _ServerHang as h:
            consec_hang += 1
            skipped_ids.extend(h.ids)
            if consec_hang >= 4:       # ~4 chunks of pure timeouts => server is down; abort county
                raise ReconciliationError(
                    f"server hanging: {consec_hang} consecutive objectIds chunks timed out",
                    {"count_status": "failed", "source_id_count": len(ids),
                     "skipped_ids": [str(value) for value in skipped_ids],
                     "skipped_count": len(skipped_ids)})
            print(f"    {label}: chunk hung (timeout), skipped {h.n} [consec {consec_hang}]",
                  flush=True)
            time.sleep(0.2)
            continue
        consec_hang = 0
        pages.extend(chunk_pages)
        skipped_ids.extend(chunk_skipped)
        print(f"    {label}: {min(i + batch, len(ids))}/{len(ids)} ids", flush=True)
        time.sleep(0.2)
    if skipped_ids:
        print(f"    {label}: SKIPPED {len(skipped_ids)} poison records "
              f"(server errors at minimum batch size)", flush=True)
    return pages, oid_field, {"source_id_count": len(ids),
                              "skipped_ids": skipped_ids}


def _fetch_oid_chunk(sess, query_url, base_params, chunk, fmt, check_field):
    """One objectIds batch -> ([GeoDataFrame...], skipped_count). A PERSISTENT server error
    (Yellowstone MT) means some record in the chunk poisons the query — bisect to isolate,
    fetch everything else, and skip only the unfetchable remainder (counted, never silent)."""
    params = dict(base_params, f=fmt)
    params.pop("where", None)
    params["objectIds"] = ",".join(str(x) for x in chunk)
    hung = server_err = False
    for attempt in range(4):
        try:
            d2, content = _getj(sess, query_url, params, post=True, tries=1, timeout=45)
        except Exception:
            hung = True               # connection/read TIMEOUT — server not responding
            time.sleep(2 * (attempt + 1))
            continue
        if isinstance(d2, dict) and d2.get("error"):
            server_err = True         # real error RESPONSE — chunk may hold a poison record
            time.sleep(2 * (attempt + 1))
            continue
        feats = d2.get("features", [])
        # some SDE joins materialize attributes INTERMITTENTLY per batch (Lane OR returned
        # 0 owners then 50/50 for the same ids seconds apart) — retry all-null batches
        attrs_key = "attributes" if fmt == "json" else "properties"
        if check_field and feats and check_field in (feats[0].get(attrs_key) or {}):
            populated = any((f.get(attrs_key) or {}).get(check_field) not in (None, "") for f in feats)
        else:
            populated = any(v not in (None, "") for f in feats
                            for k, v in (f.get(attrs_key) or {}).items()
                            if k.lower() not in ("objectid", "fid"))
        if feats and not populated and attempt < 3:
            time.sleep(2 * (attempt + 1))
            continue
        return ([_page_to_gdf(content, fmt)] if feats else []), []
    # A HANGING server (only timeouts, never an error response) makes bisection pathological —
    # each half hangs too, multiplying 45s timeouts exponentially. Bisect ONLY to isolate a
    # poison RECORD (real error responses); on a pure hang, signal the caller to skip/abort.
    if hung and not server_err:
        raise _ServerHang(chunk)
    if len(chunk) <= 25:
        return [], list(chunk)  # poison isolated to <=25 records; preserve exact source IDs
    mid = len(chunk) // 2
    lp, ls = _fetch_oid_chunk(sess, query_url, base_params, chunk[:mid], fmt, check_field)
    rp, rs = _fetch_oid_chunk(sess, query_url, base_params, chunk[mid:], fmt, check_field)
    return lp + rp, ls + rs


def _fetch_attr_table(sess, query_url, label, where="1=1"):
    """Pull a non-spatial table (the arcgis-join owner tables) -> DataFrame. `where` lets a
    statewide-sliced join pull only the current county's table rows (ND: CountyFIPS=...) instead
    of the full statewide table once per county."""
    base = query_url.rsplit("/query", 1)[0]
    meta, _ = _getj(sess, base, {"f": "json"})
    page = max(1, min(2000, int(meta.get("maxRecordCount") or 1000)))
    supports = bool(meta.get("advancedQueryCapabilities", {}).get("supportsPagination")
                    or meta.get("supportsPagination"))
    oid = meta.get("objectIdField") or "OBJECTID"
    rows = []
    if supports:
        offset = 0
        while True:
            d, _ = _getj(sess, query_url, {"where": where, "outFields": "*", "returnGeometry": "false",
                                           "f": "json", "resultOffset": offset,
                                           "resultRecordCount": page, "orderByFields": oid})
            feats = d.get("features", [])
            if not feats:
                break
            rows.extend(f["attributes"] for f in feats)
            offset += len(feats)
            print(f"    {label}: table {offset} rows", flush=True)
            time.sleep(0.2)
    else:
        d, _ = _getj(sess, query_url, {"where": where, "returnIdsOnly": "true", "f": "json"})
        ids = d.get("objectIds") or []
        step = max(1, min(500, page))
        for i in range(0, len(ids), step):
            params = {"objectIds": ",".join(str(x) for x in ids[i:i + step]),
                      "outFields": "*", "returnGeometry": "false", "f": "json"}
            d2, _ = _getj(sess, query_url, params, post=True)
            rows.extend(f["attributes"] for f in d2.get("features", []))
            print(f"    {label}: table {min(i + step, len(ids))}/{len(ids)} ids", flush=True)
            time.sleep(0.2)
    return pd.DataFrame(rows)


def _join_table(gdf, row, sess, label):
    """arcgis-join: merge the owner table onto the geometry pull. join_key is
    'geomfield=tablefield' ('=' optional when both sides share the name). Table rows with a
    populated first-owner-field win when a key has several (multi-account parcels)."""
    jkey = row.get("join_key") or ""
    gfield, _, tfield = jkey.partition("=")
    tfield = tfield or gfield
    # slice the owner table to the same county the geometry pull used (when the join layer is a
    # statewide one — ND); falls back to the full table for un-sliced joins.
    tbl = _fetch_attr_table(sess, row["join_url"], label, where=_statewide_where(row) or "1=1")
    if gfield not in gdf.columns or tfield not in tbl.columns:
        raise RuntimeError(f"join fields missing: {gfield} in geometry={gfield in gdf.columns}, "
                           f"{tfield} in table={tfield in tbl.columns}")
    first_owner = (row.get("owner_fields") or "").split("|")[0]
    if first_owner in tbl.columns:
        tbl = tbl.sort_values(first_owner, key=lambda s: s.isna() | (s.astype(str).str.strip() == ""))
    tbl = tbl.drop_duplicates(subset=[tfield])
    key = gdf[gfield].astype(str).str.strip()
    tbl[tfield] = tbl[tfield].astype(str).str.strip()
    merged = gdf.merge(tbl, how="left", left_on=key.rename("_k"), right_on=tfield,
                       suffixes=("", "_tbl"))
    merged = merged.drop(columns=[c for c in ("_k",) if c in merged.columns])
    matched = merged[tfield].notna().mean()
    print(f"    {label}: joined owner table — {matched:.0%} of parcels matched", flush=True)
    return gpd.GeoDataFrame(merged, geometry=gdf.geometry.name, crs=gdf.crs)


def _fetch_wfs(sess, base_url, typename, county, label, step=10000):
    """Page a GeoServer WFS 2.0 county slice (count+startIndex) -> list of GeoDataFrames."""
    cql = "county='{}'".format(county.replace("'", "''"))
    pages, start = [], 0
    while True:
        params = {"service": "WFS", "version": "2.0.0", "request": "GetFeature",
                  "typeNames": typename, "outputFormat": "application/json", "srsName": "EPSG:4326",
                  "CQL_FILTER": cql, "count": step, "startIndex": start}
        r = sess.get(base_url, params=params, headers=UA, timeout=240)
        feats = r.json().get("features", [])
        if not feats:
            break
        pages.append(gpd.GeoDataFrame.from_features(feats, crs=4326))
        start += len(feats)
        print(f"    {label}: {start} features", flush=True)
        if len(feats) < step:
            break
        time.sleep(0.2)
    return pages


def _ingest_wfs_county(row, raw_dir, refresh=False, count_waiver_reason=None):
    """GeoServer WFS extractor (OK statewide okmaps.org) — the `wfs` crosswalk method."""
    fips, county, state = row["fips"], row["county"], row["state"]
    slug = re.sub(r"[^a-z0-9]+", "_", county.lower()).strip("_")
    out_path = os.path.join(raw_dir, f"{fips}_{slug}.parquet")
    label = f"{state} {county} {fips}"
    if os.path.exists(out_path) and not refresh:
        print(f"  SKIP {label} (exists; --refresh to re-pull)")
        return None
    base_url = row["query_url"].split("?")[0]
    m = re.search(r"typeNames=([^&]+)", row["query_url"])
    typename = m.group(1) if m else "current_statewide_parcels"
    sess = requests.Session()

    def _hits(cnty):
        try:
            rr = sess.get(base_url, headers=UA, timeout=60, params={
                "service": "WFS", "version": "2.0.0", "request": "GetFeature", "typeNames": typename,
                "CQL_FILTER": "county='{}'".format(cnty.replace("'", "''")), "resultType": "hits"})
            mm = re.search(r'numberMatched="(\d+)"', rr.text)
            return int(mm.group(1)) if mm else None
        except Exception:
            return None

    cnty_val, count = county, _hits(county)
    if not count and county.replace(" ", "") != county:  # some WFS values strip spaces (Le Flore->LeFlore)
        if _hits(county.replace(" ", "")):
            cnty_val, count = county.replace(" ", ""), _hits(county.replace(" ", ""))
    print(f"  PULL {label}: ~{count if count is not None else '?'} parcels (wfs)", flush=True)
    pages = _fetch_wfs(sess, base_url, typename, cnty_val, label)
    if not pages:
        raise RuntimeError("no features returned (wfs)")
    gdf = gpd.GeoDataFrame(pd.concat(pages, ignore_index=True), geometry="geometry", crs=4326)
    for c in gdf.columns:
        if c != gdf.geometry.name and gdf[c].dtype == object:
            gdf[c] = gdf[c].astype("string")
    try:
        audit = reconcile_feature_counts(
            len(gdf), reported_count=count, waiver_reason=count_waiver_reason)
    except ReconciliationError as exc:
        audit = dict(exc.audit, fips=fips, source_url=row["query_url"], fetch_method="wfs",
                     ingest_status="failed", error=str(exc))
        audit_file = write_ingest_audit(ROOT, raw_dir, fips, audit)
        raise RuntimeError(f"{exc}; audit={audit_file}") from exc
    os.makedirs(raw_dir, exist_ok=True)
    atomic_replace_writer(
        out_path, lambda temp: gdf.to_parquet(temp, compression="zstd"))
    audit.update({"fips": fips, "source_url": row["query_url"], "fetch_method": "wfs",
                  "ingest_status": "completed"})
    audit_file = write_ingest_audit(ROOT, raw_dir, fips, audit)
    print(f"  DONE {label}: {len(gdf)} parcels -> {os.path.relpath(out_path, ROOT)}", flush=True)
    entry = {
        "fips": fips, "county": county, "state": state,
        "file": os.path.relpath(out_path, ROOT).replace("\\", "/"),
        "feature_count": int(len(gdf)), "reported_count": count,
        "expected_count": audit["expected_count"], "fetched_count": audit["fetched_count"],
        "skipped_count": audit["skipped_count"], "count_delta": audit["count_delta"],
        "count_status": audit["count_status"], "ingest_audit_file": audit_file,
        "retrieved_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source_last_edit": None, "source_url": row["query_url"],
        "owner_fields": row["owner_fields"], "value_field": row["value_field"], "note": row["note"],
    }
    if audit.get("count_waiver_reason"):
        entry["count_waiver_reason"] = audit["count_waiver_reason"]
    return entry


def ingest_county(row, raw_dir, refresh=False, count_waiver_reason=None):
    """Download one county's full parcel layer -> GeoParquet. Returns a manifest entry."""
    if row.get("method") == "wfs":
        return _ingest_wfs_county(row, raw_dir, refresh, count_waiver_reason)
    fips, county, state = row["fips"], row["county"], row["state"]
    slug = re.sub(r"[^a-z0-9]+", "_", county.lower()).strip("_")
    out_path = os.path.join(raw_dir, f"{fips}_{slug}.parquet")
    label = f"{state} {county} {fips}"

    if os.path.exists(out_path) and not refresh:
        print(f"  SKIP {label} (exists; --refresh to re-pull)")
        return None

    query_url = row["query_url"]
    base_url = query_url.rsplit("/query", 1)[0]
    sess = requests.Session()

    meta, _ = _getj(sess, base_url, {"f": "json"})
    if meta.get("error"):
        raise RuntimeError(f"layer metadata error: {meta['error'].get('message')}")
    page = max(1, min(2000, int(meta.get("maxRecordCount") or 1000)))
    for sub, ps in STATEWIDE_PAGESIZE.items():  # raise the cap for known-slow large-page layers
        if sub in (row.get("query_url") or ""):
            page = min(ps, int(meta.get("maxRecordCount") or ps))
    supports_pag = bool(meta.get("advancedQueryCapabilities", {}).get("supportsPagination")
                        or meta.get("supportsPagination"))
    oid_field = meta.get("objectIdField") or "OBJECTID"
    last_edit_ms = (meta.get("editingInfo") or {}).get("lastEditDate")
    last_edit = (datetime.datetime.fromtimestamp(last_edit_ms / 1000, datetime.timezone.utc)
                 .strftime("%Y-%m-%d") if last_edit_ms else None)

    # optional per-row where filter: lets one STATEWIDE layer serve single-county pulls
    where = row.get("where") or _statewide_where(row) or "1=1"
    count = None
    try:
        d, _ = _getj(sess, query_url, {"where": where, "returnCountOnly": "true", "f": "json"})
        count = d.get("count")
    except Exception as e:
        # count-queries time out on some big layers (known census artifact); paging still works —
        # but without a count the under-fetch guard below is BLIND, so never swallow this silently
        print(f"    WARNING {label}: count query failed ({e}); will retry post-fetch", flush=True)

    base_params = {"where": where, "outFields": _statewide_outfields(row), "outSR": 4326}
    if supports_pag:
        # orderBy makes resultOffset paging stable (rows can shift between unordered pages)
        base_params["orderByFields"] = oid_field

    print(f"  PULL {label}: ~{count if count is not None else '?'} parcels, page={page}, "
          f"pagination={supports_pag}, lastEdit={last_edit}", flush=True)

    # Escalating fetch paths: offset pagination (geojson, then Esri JSON), then objectIds
    # batches. The all-null check matters: some SDE joins (Lane OR CommishSearch) serve real
    # attributes ONLY for objectIds queries and NULL everything under offset pagination.
    attempts = ([("pag", "geojson"), ("pag", "json"), ("oids", "json")] if supports_pag
                else [("oids", "geojson"), ("oids", "json")])
    pages = []
    fetch_audit = {"source_id_count": None, "skipped_ids": []}
    for mode, fmt in attempts:
        try:
            if mode == "pag":
                pages = _fetch_pages(sess, query_url, base_params, page, fmt, count, label)
                fetch_audit = {"source_id_count": None, "skipped_ids": []}
                got = sum(len(p) for p in pages)
                if count is not None and pages and oid_field in pages[0].columns:
                    # Unstable server ordering makes offset pages OVERLAP: Madison KY returns
                    # 39,129 rows of which only 37,034 distinct OIDs (2,095 repeated, 2,095 never
                    # served) even with orderByFields. Count distinct IDs, not rows.
                    got = min(got, int(pd.concat([p[oid_field] for p in pages
                                                  if oid_field in p.columns]).nunique()))
                if count is not None and got < count:
                    # Escalate to object-ID batches instead of failing reconciliation after the
                    # fact (2026-09-14; same class at Lowndes GA 51,424/66,316, Alleghany VA).
                    raise RuntimeError(f"offset paging under-fetched {got:,} distinct/{count:,}")
            else:
                first_owner = (row.get("owner_fields") or "").split("|")[0] or None
                pages, oid_field, fetch_audit = _fetch_by_objectids(
                    sess, query_url, base_params, meta, fmt, label, check_field=first_owner)
                if fetch_audit["skipped_ids"]:
                    raise ReconciliationError(
                        f"object-ID fetch skipped {len(fetch_audit['skipped_ids'])} source IDs",
                        dict(fetch_audit, count_status="failed"))
            fetch_audit.update({"fetch_method": mode, "fetch_format": fmt})
            if pages:
                s = pages[0]
                attrs = [c for c in s.columns if c != s.geometry.name and c.lower() != "objectid"]
                if attrs and int(s[attrs].notna().sum().sum()) == 0:
                    raise RuntimeError(f"{mode}/{fmt} returned all-null attributes")
            break
        except Exception as e:
            if (mode, fmt) == attempts[-1]:
                failed_audit = dict(getattr(e, "audit", fetch_audit), fips=fips,
                                    source_url=query_url, fetch_method=mode, fetch_format=fmt,
                                    ingest_status="failed", error=str(e))
                audit_file = write_ingest_audit(ROOT, raw_dir, fips, failed_audit)
                raise RuntimeError(f"{e}; audit={audit_file}") from e
            print(f"    {mode}/f={fmt} failed ({e}); trying next path", flush=True)

    if not pages:
        raise RuntimeError("no features returned")
    # pages can carry EQUIVALENT-but-not-identical CRS objects (Davidson TN: two 'WGS 84'
    # variants) which breaks concat — we requested outSR=4326 everywhere, so unify
    pages = [p.set_crs(4326, allow_override=True) for p in pages]
    gdf = pd.concat(pages, ignore_index=True)
    if gdf.columns.duplicated().any():
        # duplicate column names (Williamson TN) make gdf[c] return a DataFrame and break
        # every per-column operation downstream — keep the first occurrence
        print(f"    {label}: dropping {int(gdf.columns.duplicated().sum())} duplicate-named columns", flush=True)
        gdf = gdf.loc[:, ~gdf.columns.duplicated()]
    if oid_field in gdf.columns:
        gdf = gdf.drop_duplicates(subset=[oid_field], ignore_index=True)
    if row.get("method") == "arcgis-join" and row.get("join_url"):
        gdf = _join_table(gdf, row, sess, label)
    # mixed types across pages (Skagit's SaleDate: int on some pages, str on others) land as
    # object dtype, which parquet can't write — unify those columns to pandas string
    for c in gdf.columns:
        if c != gdf.geometry.name and gdf[c].dtype == object:
            gdf[c] = gdf[c].astype("string")
    if count is None:
        try:  # second chance now that the host is warmed up — re-arms the under-fetch guard
            d, _ = _getj(sess, query_url, {"where": where, "returnCountOnly": "true", "f": "json"})
            count = d.get("count")
        except Exception:
            print(f"    WARNING {label}: source count UNVERIFIABLE — a truncated pull would be "
                  f"invisible without a reviewed waiver", flush=True)
    try:
        audit = reconcile_feature_counts(
            len(gdf), reported_count=count,
            source_id_count=fetch_audit.get("source_id_count"),
            skipped_ids=fetch_audit.get("skipped_ids"),
            waiver_reason=count_waiver_reason)
    except ReconciliationError as exc:
        failed_audit = dict(exc.audit, fips=fips, source_url=query_url,
                            fetch_method=fetch_audit.get("fetch_method"),
                            fetch_format=fetch_audit.get("fetch_format"),
                            ingest_status="failed", error=str(exc))
        audit_file = write_ingest_audit(ROOT, raw_dir, fips, failed_audit)
        raise RuntimeError(f"{exc}; audit={audit_file}") from exc

    os.makedirs(raw_dir, exist_ok=True)
    atomic_replace_writer(
        out_path, lambda temp: gdf.to_parquet(temp, compression="zstd"))
    audit.update({"fips": fips, "source_url": query_url,
                  "fetch_method": fetch_audit.get("fetch_method"),
                  "fetch_format": fetch_audit.get("fetch_format"),
                  "ingest_status": "completed"})
    audit_file = write_ingest_audit(ROOT, raw_dir, fips, audit)
    print(f"  DONE {label}: {len(gdf)} parcels -> {os.path.relpath(out_path, ROOT)}", flush=True)
    entry = {
        "fips": fips, "county": county, "state": state,
        "file": os.path.relpath(out_path, ROOT).replace("\\", "/"),
        "feature_count": int(len(gdf)), "reported_count": count,
        "expected_count": audit["expected_count"], "fetched_count": audit["fetched_count"],
        "skipped_count": audit["skipped_count"], "count_delta": audit["count_delta"],
        "count_status": audit["count_status"], "ingest_audit_file": audit_file,
        "retrieved_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source_last_edit": last_edit, "source_url": query_url,
        "owner_fields": row["owner_fields"], "value_field": row["value_field"],
        "note": row["note"],
    }
    if audit.get("count_waiver_reason"):
        entry["count_waiver_reason"] = audit["count_waiver_reason"]
    return entry


def load_manifest(path):
    if not os.path.exists(path):
        return {}
    # Retry transient concurrent-access failures: under parallel lanes a read can collide with
    # another writer's os.replace (Windows WinError 5/13/32) or briefly catch a partial file.
    # Without this, an unlucky read crashes the caller -> a good county gets marked failed.
    for _ in range(100):
        try:
            with open(path, encoding="utf-8") as f:
                data = f.read()               # read bytes then CLOSE before the (slow) parse, so the
            return json.loads(data)           # file-hold window stays ~ms -> minimal replace conflict
        except (OSError, ValueError):
            time.sleep(0.05)
    with open(path, encoding="utf-8") as f:   # final attempt — surface a real, persistent error
        data = f.read()
    return json.loads(data)


def update_manifest(fips, updates, manifest_path=None):
    """The ONLY safe way to write the manifest: lockfile-guarded read-modify-write.
    Concurrent state ingests lost entries to write races (Gallatin MT) — every writer
    (ingest/normalize/bulk/refresh/landuse) must go through here."""
    path = manifest_path or os.path.join(OUT_DIR, "manifest.json")
    lock = path + ".lock"
    fd = None
    for _ in range(1200):
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            time.sleep(0.1)
    if fd is None:
        raise RuntimeError("manifest lock timeout (stale .lock file?)")
    try:
        manifest = load_manifest(path)
        manifest.setdefault(fips, {}).update(updates)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # ATOMIC write: temp + os.replace (atomic on Windows, same volume) so unlocked READERS
        # (rollout driver, normalize/owners_std startup) never see a half-written file. The lock
        # only serializes writers; without atomic replace, concurrent lanes hit JSONDecodeError.
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=1, sort_keys=True)
        for _ in range(600):                 # os.replace fails on Windows if a reader holds the file
            try:                             # open (WinError 5/32 — no FILE_SHARE_DELETE); readers
                os.replace(tmp, path)        # open-read-close fast, so retrying ~30s wins under the
                break                        # real (infrequent) read load. Atomic on same volume.
            except OSError:
                time.sleep(0.05)
        else:
            os.replace(tmp, path)            # final attempt — let it raise if truly stuck
    finally:
        os.close(fd)
        os.unlink(lock)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--state", help="two-letter state code, e.g. WA")
    ap.add_argument("--fips", nargs="*", help="specific county fips codes")
    ap.add_argument("--limit", type=int, help="stop after N counties (smoke test)")
    ap.add_argument("--refresh", action="store_true", help="re-pull counties that already have a file")
    ap.add_argument("--count-waiver-reason",
                    help="reviewed reason allowed only when the source exposes no count or ID universe; "
                         "mismatches and skipped IDs cannot be waived")
    args = ap.parse_args()

    with open(CROSSWALK, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    targets = [r for r in rows if r["method"] == "arcgis"
               or (r["method"] == "arcgis-join" and r.get("join_url"))
               or r["method"] == "wfs"]
    if args.state:
        targets = [r for r in targets if r["state"].upper() == args.state.upper()]
    if args.fips:
        want = set(args.fips)
        targets = [r for r in targets if r["fips"] in want]
    if args.limit:
        targets = targets[:args.limit]
    if not targets:
        sys.exit("no matching arcgis counties — check --state/--fips (only method=arcgis is bulk-extractable)")

    raw_dir = os.path.join(OUT_DIR, "raw")
    manifest_path = os.path.join(OUT_DIR, "manifest.json")
    manifest = load_manifest(manifest_path)

    print(f"Ingesting {len(targets)} counties -> {os.path.relpath(raw_dir, ROOT)}")
    ok, failed = 0, []
    for row in targets:
        try:
            entry = ingest_county(row, raw_dir, refresh=args.refresh,
                                  count_waiver_reason=args.count_waiver_reason)
            if entry:
                update_manifest(row["fips"], entry, manifest_path)
            ok += 1
        except Exception as e:
            failed.append((row["fips"], row["county"], str(e)))
            print(f"  FAIL {row['state']} {row['county']} {row['fips']}: {e}", flush=True)

    print(f"\n{ok} ok, {len(failed)} failed of {len(targets)}")
    for fips, county, err in failed:
        print(f"  FAILED {fips} {county}: {err[:200]}")
    if failed:
        sys.exit(1)  # partial batches must be visible to callers/orchestration, not exit-0


if __name__ == "__main__":
    main()
