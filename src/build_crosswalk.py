"""
Build the county -> owner-resolution-method CROSSWALK (the routing index that powers
item2 location->owner lookups). Seeded from every verified probe we already have.

method:
  arcgis        -> owner in a live ArcGIS REST layer; point-in-parcel queryable (the good case)
  bulk-download -> owner only in a downloadable GDB/shapefile (no live point query)
  deed-records  -> owner not in a layer; reconstruct from bulk deed/sales records (King)
  suppressed    -> owner withheld from the public REST layer (RCW); value+geom only (Clark, Pierce)
  none          -> no public programmatic source

Output: src/county_methods.csv  (fips, county, state, method, point_query, owner_fields, query_url)
"""
import os, csv, json, glob, re

HERE = os.path.dirname(__file__)
ROOT = os.path.dirname(HERE)
OUT = os.path.join(HERE, "county_methods.csv")

def clean_query(url):
    if not url: return ""
    url = url.split("?")[0]
    i = url.find("/query")
    if i >= 0: return url[:i] + "/query"
    return url.rstrip("/") + "/query"

def owner_list(s):
    return [t.strip() for t in re.split(r"[,/]", s or "") if t.strip()]

def method_from(j):
    if str(j.get("has_public_endpoint","")).upper() != "Y": return "none", "N"
    et = (j.get("endpoint_type") or "").lower()
    if "bulk" in et: return "bulk-download", "N"
    if str(j.get("owner_name","")).upper() == "Y": return "arcgis", "Y"
    blob = (j.get("quirks","")+j.get("notes","")+j.get("evidence","")+j.get("access_class","")).lower()
    if "erealproperty" in blob or "deed" in blob or "grantee" in blob: return "deed-records", "N"
    return "suppressed", "N"

# ---- 33 probe JSONs (have owner_field + endpoint_url) ----
rows = {}
for f in glob.glob(os.path.join(ROOT, "data", "probes", "53*.json")):  # WA probes are FIPS 53xxx
    j = json.load(open(f, encoding="utf-8"))
    m, pq = method_from(j)
    rows[j["fips"]] = {
        "fips": j["fips"], "county": j["county"], "state": "WA",
        "method": m, "point_query": pq,
        "owner_fields": "|".join(owner_list(j.get("owner_field",""))),
        "query_url": clean_query(j.get("endpoint_url","")) if m == "arcgis" else "",
    }

# ---- probes_west: per-county crosswalk probes (already crosswalk-shaped) ----
for f in glob.glob(os.path.join(ROOT, "data", "probes_west", "*.json")):
    try:
        j = json.load(open(f, encoding="utf-8"))
    except (ValueError, OSError):
        continue                                  # skip unparseable scratch files, don't crash the build
    if not re.match(r"^\d{5}$", str(j.get("fips", ""))):
        continue                                  # skip non-probe files (curl dumps etc. lacking a real fips)
    of = j.get("owner_fields") or []
    if isinstance(of, str): of = owner_list(of)
    rows[j["fips"]] = {
        "fips": j["fips"], "county": j["county"], "state": j.get("state", ""),
        "method": j.get("method", ""), "point_query": j.get("point_query", "N"),
        "owner_fields": "|".join(of),
        "query_url": clean_query(j.get("query_url", "")) if j.get("query_url") else "",
        "value_field": j.get("value_field") or "", "join_key": j.get("join_key") or "",
        # carry note/join_url so per-county caveats (e.g. "city-only coverage") reach resolve() output
        "note": j.get("note") or "",
        "join_url": clean_query(j.get("join_url", "")) if j.get("join_url") else "",
    }

# ---- the 6 original counties (owner fields known from the pull scripts) + a Schneider county ----
SEED = {
    "53061": ("Snohomish","WA","arcgis","Y",["OWNERNAME"],"https://services6.arcgis.com/z6WYi9VRHfgwgtyW/arcgis/rest/services/Parcels/FeatureServer/0/query"),
    "53007": ("Chelan","WA","arcgis","Y",["Owner_Name"],"https://services1.arcgis.com/cQqftPrF7IjnPbra/arcgis/rest/services/04_02_26_CCParcels/FeatureServer/0/query"),
    "53057": ("Skagit","WA","arcgis","Y",["OwnerName"],"https://gis.skagitcountywa.gov/arcgis/rest/services/OpenData/AssessorDataParcels/MapServer/0/query"),
    "53055": ("San Juan","WA","arcgis","Y",["Owner"],"https://gis.sanjuancountywa.gov/arcgis/rest/services/Polaris/Parcels/MapServer/0/query"),
    "53073": ("Whatcom","WA","arcgis","Y",["title_owner_name","tax_payer_name"],"https://gis.whatcomcounty.us/arcgis/rest/services/EnterprisePublishing/WhatcomCo_Property/MapServer/1/query"),  # owner fields corrected 2026-06-08 (OWNER/TAXPAYER no longer exist)
    "53033": ("King","WA","deed-records","N",[],""),
    "27079": ("Le Sueur","MN","arcgis","Y",["OwnerName","OwnerName2"],"https://wfs.schneidercorp.com/arcgis/rest/services/LeSueurCountyMN_WFS/MapServer/1/query"),
}
for fips,(county,state,m,pq,of,url) in SEED.items():
    rows[fips] = {"fips":fips,"county":county,"state":state,"method":m,"point_query":pq,
                  "owner_fields":"|".join(of),"query_url":url}

# ---- IDAHO: one statewide owner POLYGON layer (IDL WhiteStar) covers all 44 counties at
# 96.8% owner-populated, point-queryable — incl. the big counties (Ada/Canyon/Benewah) that
# strip owner from their OWN REST. So route all 44 ID counties to WhiteStar (overrides the
# per-county probes, which remain on disk as reference). ----
WHITESTAR = "https://gis1.idl.idaho.gov/arcgis/rest/services/Portal/WhiteStar_Parcels/FeatureServer/0/query"
ID_ALL = {
 "16001":"Ada","16003":"Adams","16005":"Bannock","16007":"Bear Lake","16009":"Benewah",
 "16011":"Bingham","16013":"Blaine","16015":"Boise","16017":"Bonner","16019":"Bonneville",
 "16021":"Boundary","16023":"Butte","16025":"Camas","16027":"Canyon","16029":"Caribou",
 "16031":"Cassia","16033":"Clark","16035":"Clearwater","16037":"Custer","16039":"Elmore",
 "16041":"Franklin","16043":"Fremont","16045":"Gem","16047":"Gooding","16049":"Idaho",
 "16051":"Jefferson","16053":"Jerome","16055":"Kootenai","16057":"Latah","16059":"Lemhi",
 "16061":"Lewis","16063":"Lincoln","16065":"Madison","16067":"Minidoka","16069":"Nez Perce",
 "16071":"Oneida","16073":"Owyhee","16075":"Payette","16077":"Power","16079":"Shoshone",
 "16081":"Teton","16083":"Twin Falls","16085":"Valley","16087":"Washington"}
for fips, name in ID_ALL.items():
    rows[fips] = {"fips": fips, "county": name, "state": "ID", "method": "arcgis",
                  "point_query": "Y", "owner_fields": "owner1|owner2", "value_field": "totalvalue",
                  "join_key": "", "query_url": WHITESTAR}

# ---- MONTANA: one statewide owner POLYGON layer (MT Dept of Revenue CAMA, served by the
# Montana State Library Cadastral Framework) covers all 56 counties at 96.0% owner-populated
# (920,595 parcels, 883,715 with OwnerName; per-county coverage uniform, none stripped),
# point-queryable. Route all 56 MT counties to it. Verified live 2026-06-06. ----
# (gisservicemt.gov/.../MSDI_Framework/Parcels died 2026-09; same-schema successor re-verified 2026-09-28)
MT_CADASTRAL = "https://gisservice.mt.gov/arcgis/rest/services/msdi_cadastral_map_v1/MapServer/1/query"
MT_ALL = {
 "30001":"Beaverhead","30003":"Big Horn","30005":"Blaine","30007":"Broadwater","30009":"Carbon",
 "30011":"Carter","30013":"Cascade","30015":"Chouteau","30017":"Custer","30019":"Daniels",
 "30021":"Dawson","30023":"Deer Lodge","30025":"Fallon","30027":"Fergus","30029":"Flathead",
 "30031":"Gallatin","30033":"Garfield","30035":"Glacier","30037":"Golden Valley","30039":"Granite",
 "30041":"Hill","30043":"Jefferson","30045":"Judith Basin","30047":"Lake","30049":"Lewis and Clark",
 "30051":"Liberty","30053":"Lincoln","30055":"McCone","30057":"Madison","30059":"Meagher",
 "30061":"Mineral","30063":"Missoula","30065":"Musselshell","30067":"Park","30069":"Petroleum",
 "30071":"Phillips","30073":"Pondera","30075":"Powder River","30077":"Powell","30079":"Prairie",
 "30081":"Ravalli","30083":"Richland","30085":"Roosevelt","30087":"Rosebud","30089":"Sanders",
 "30091":"Sheridan","30093":"Silver Bow","30095":"Stillwater","30097":"Sweet Grass","30099":"Teton",
 "30101":"Toole","30103":"Treasure","30105":"Valley","30107":"Wheatland","30109":"Wibaux",
 "30111":"Yellowstone"}
for fips, name in MT_ALL.items():
    rows[fips] = {"fips": fips, "county": name, "state": "MT", "method": "arcgis",
                  "point_query": "Y", "owner_fields": "OwnerName", "value_field": "TotalValue",
                  "join_key": "", "query_url": MT_CADASTRAL}

# ---- WYOMING: one statewide owner POLYGON layer (WY Dept of Revenue, assembled from the 23
# county assessors) covers all 23 counties at 89.1% owner-populated (373,048 parcels, 332,387
# with ownername1; per-county 66-97%, none stripped), point-queryable. Route all 23 WY counties
# to it. Verified live 2026-06-06. NOTE: 2025 snapshot; OID field is FID not OBJECTID. ----
WY_PARCELS = "https://services3.arcgis.com/r0iJ85SKZ4zAzz3P/arcgis/rest/services/Wyoming_Parcels_for_2025/FeatureServer/0/query"
WY_ALL = {
 "56001":"Albany","56003":"Big Horn","56005":"Campbell","56007":"Carbon","56009":"Converse",
 "56011":"Crook","56013":"Fremont","56015":"Goshen","56017":"Hot Springs","56019":"Johnson",
 "56021":"Laramie","56023":"Lincoln","56025":"Natrona","56027":"Niobrara","56029":"Park",
 "56031":"Platte","56033":"Sheridan","56035":"Sublette","56037":"Sweetwater","56039":"Teton",
 "56041":"Uinta","56043":"Washakie","56045":"Weston"}
for fips, name in WY_ALL.items():
    rows[fips] = {"fips": fips, "county": name, "state": "WY", "method": "arcgis",
                  "point_query": "Y", "owner_fields": "ownername1|ownername2", "value_field": "actualvalu",
                  "join_key": "", "query_url": WY_PARCELS}

# ---- NEW MEXICO: statewide owner layer hosted by the NM Office of the State Engineer
# (County_Parcels_2025), authored by NM Taxation & Revenue Property Tax Div + the 33 county
# assessors. 1.59M parcels, 92.3% owner. BUT it's 33 SIBLING layers (one per county, ids 0-32,
# alphabetical) under one MapServer, NOT a merged layer -> route each county to its own layer id.
# owner=OwnerAll (combined "Owner1 | Owner2"). NO value field on this layer. Verified live 2026-06-06. ----
NM_BASE = "https://gis.ose.nm.gov/server_s/rest/services/Parcels/County_Parcels_2025/MapServer"
NM_ALL = {
 "35001":("Bernalillo",0),"35003":("Catron",1),"35005":("Chaves",2),"35006":("Cibola",3),
 "35007":("Colfax",4),"35009":("Curry",5),"35011":("De Baca",6),"35013":("Doña Ana",7),
 "35015":("Eddy",8),"35017":("Grant",9),"35019":("Guadalupe",10),"35021":("Harding",11),
 "35023":("Hidalgo",12),"35025":("Lea",13),"35027":("Lincoln",14),"35028":("Los Alamos",15),
 "35029":("Luna",16),"35031":("McKinley",17),"35033":("Mora",18),"35035":("Otero",19),
 "35037":("Quay",20),"35039":("Rio Arriba",21),"35041":("Roosevelt",22),"35043":("Sandoval",23),
 "35045":("San Juan",24),"35047":("San Miguel",25),"35049":("Santa Fe",26),"35051":("Sierra",27),
 "35053":("Socorro",28),"35055":("Taos",29),"35057":("Torrance",30),"35059":("Union",31),
 "35061":("Valencia",32)}
for fips,(name,lid) in NM_ALL.items():
    rows[fips] = {"fips":fips,"county":name,"state":"NM","method":"arcgis","point_query":"Y",
                  "owner_fields":"OwnerAll","value_field":"","join_key":"",
                  "query_url":f"{NM_BASE}/{lid}/query"}

# ---- COLORADO: the statewide layer (CO OIT, gis.colorado.gov Colorado_Public_Parcels) covers 44
# of 64 counties; owner populated for 41 (85.5% statewide). El Paso/Summit/Washington are present
# but owner 100% NULL, and 20 counties are absent -> those are handled per-county (probes_west) or
# marked unprobed. Route the 41 owner-populated counties to the statewide layer. owner=owner|owner2,
# value=apprValTot. Verified live 2026-06-06. ----
CO_PARCELS = "https://gis.colorado.gov/public/rest/services/Address_and_Parcel/Colorado_Public_Parcels/FeatureServer/0/query"
CO_STATEWIDE = {
 "08001":"Adams","08005":"Arapahoe","08007":"Archuleta","08013":"Boulder","08014":"Broomfield",
 "08019":"Clear Creek","08027":"Custer","08031":"Denver","08033":"Dolores","08035":"Douglas",
 "08037":"Eagle","08039":"Elbert","08045":"Garfield","08047":"Gilpin","08049":"Grand",
 "08051":"Gunnison","08053":"Hinsdale","08055":"Huerfano","08059":"Jefferson","08065":"Lake",
 "08067":"La Plata","08069":"Larimer","08075":"Logan","08077":"Mesa","08081":"Moffat",
 "08083":"Montezuma","08087":"Morgan","08091":"Ouray","08093":"Park","08097":"Pitkin",
 "08101":"Pueblo","08103":"Rio Blanco","08105":"Rio Grande","08107":"Routt","08109":"Saguache",
 "08111":"San Juan","08113":"San Miguel","08115":"Sedgwick","08119":"Teller","08123":"Weld",
 "08125":"Yuma"}
for fips,name in CO_STATEWIDE.items():
    rows[fips] = {"fips":fips,"county":name,"state":"CO","method":"arcgis","point_query":"Y",
                  "owner_fields":"owner|owner2","value_field":"apprValTot","join_key":"",
                  "query_url":CO_PARCELS}
# CO counties absent from the statewide layer AND not individually probed -> unprobed (NOT presumed).
CO_UNPROBED = {
 "08003":"Alamosa","08009":"Baca","08011":"Bent","08017":"Cheyenne","08021":"Conejos",
 "08023":"Costilla","08025":"Crowley","08057":"Jackson","08061":"Kiowa","08063":"Kit Carson",
 "08071":"Las Animas","08073":"Lincoln","08079":"Mineral","08089":"Otero","08095":"Phillips",
 "08099":"Prowers"}
for fips,name in CO_UNPROBED.items():
    if fips not in rows:
        rows[fips] = {"fips":fips,"county":name,"state":"CO","method":"unprobed","point_query":"N",
                      "owner_fields":"","value_field":"","join_key":"","query_url":""}

# ==== STATEWIDE-SWEEP JACKPOTS (Wave 1, 2026-06-07) — route whole states to one owner layer. ====
# Enumerate each state's county FIPS from the US county geometry (avoids hand-typing hundreds of
# FIPS). The resolver does point-in-parcel, so a single statewide layer serves every county.
US_GEO = os.path.join(ROOT, "data", "us_counties.geojson")
STATE_FIPS = {}  # 2-digit state fips -> {county_fips: county_name}
for ft in json.load(open(US_GEO, encoding="utf-8"))["features"]:
    cf = ft.get("id")
    if cf:
        STATE_FIPS.setdefault(cf[:2], {})[cf] = ft.get("properties", {}).get("NAME", "")

def route_state(sfips, abbr, url, owner, value="", only=None, exclude=None, method="arcgis", pq="Y", note="", join_key="", join_url=""):
    exclude = exclude or set()
    for cf, name in STATE_FIPS.get(sfips, {}).items():
        if only is not None and cf not in only: continue
        if cf in exclude: continue
        rows[cf] = {"fips": cf, "county": name, "state": abbr, "method": method, "point_query": pq,
                    "owner_fields": owner, "value_field": value, "join_key": join_key, "query_url": url,
                    "note": note, "join_url": join_url}

def mark_unprobed(sfips, abbr, fips_iter):
    for cf in fips_iter:
        if cf in rows: continue   # don't clobber a real per-county probe (probes_west loads first)
        rows[cf] = {"fips": cf, "county": STATE_FIPS.get(sfips, {}).get(cf, ""), "state": abbr,
                    "method": "unprobed", "point_query": "N", "owner_fields": "", "value_field": "",
                    "join_key": "", "query_url": "", "note": "", "join_url": ""}

# Clean jackpots — one statewide owner layer, all counties (verified live 2026-06-07):
route_state("12", "FL", "https://services9.arcgis.com/Gh9awoU677aKree0/arcgis/rest/services/Florida_Statewide_Cadastral/FeatureServer/0/query", "OWN_NAME", "JV")            # FDOR Cadastral, 99.1%
route_state("55", "WI", "https://services3.arcgis.com/n6uYoouQZW75n5WI/arcgis/rest/services/Wisconsin_Statewide_Parcels_DB/FeatureServer/0/query",  # V12 2026; old service 'Invalid URL' 2026-09 "OWNERNME1|OWNERNME2", "CNTASSDVALUE")  # WI DOA, 95.9% (OWNERNME1='NOT AVAILABLE' = withheld marker on ~2.5%)
route_state("25", "MA", "https://arcgisserver.digital.mass.gov/arcgisserver/rest/services/AGOL/L3Parcels_feature_service/MapServer/0/query", "OWNER1", "TOTAL_VAL")          # MassGIS L3, 99.2%
route_state("37", "NC", "https://services.nconemap.gov/secure/rest/services/NC1Map_Parcels/MapServer/1/query", "ownname", "parval")                                          # NC OneMap, 99% (Hoke blank at source)
route_state("05", "AR", "https://geostor.arkansas.gov/arcgis/rest/services/FEATURESERVICES/Planning_Cadastre/MapServer/6/query", "ownername", "totalvalue")                  # AR GeoStor, 99.99% (broken TLS -> resolver SSL fallback)

# TN PARTIAL: statewide 'public use' layer covers 86 counties (~100% owner); 9 metros self-host.
TN_SELFHOSTED = {"47037","47157","47093","47065","47149","47187","47125","47081","47023"}  # Davidson,Shelby,Knox,Hamilton,Rutherford,Williamson,Montgomery,Hickman,Chester
route_state("47", "TN", "https://services1.arcgis.com/YuVBSS7Y1of2Qud1/arcgis/rest/services/Tennessee_Property_Boundaries_Public_Use/FeatureServer/0/query", "OWNER|OWNER2", "", exclude=TN_SELFHOSTED)
rows["47037"] = {"fips":"47037","county":"Davidson","state":"TN","method":"arcgis","point_query":"Y",  # Nashville on its own REST (verified)
                 "owner_fields":"Owner","value_field":"","join_key":"",
                 "query_url":"https://maps.nashville.gov/arcgis/rest/services/Cadastral/Parcels/MapServer/0/query"}
for cf in (TN_SELFHOSTED - {"47037"}):  # other 8 self-hosted metros -> per-county wave
    if cf in rows: continue   # don't clobber a per-county probe (probes_west loaded first) -- 2026-06-08: unprobed_sweep added Montgomery/Rutherford/Williamson arcgis
    rows[cf] = {"fips":cf,"county":STATE_FIPS["47"].get(cf,""),"state":"TN","method":"unprobed",
                "point_query":"N","owner_fields":"","value_field":"","join_key":"","query_url":""}

# MN PARTIAL: the opt-in proxy (plan_parcels_open) carries owner_name for 36 of 87 counties. Verified LIVE per-county
# 2026-06-07 (groupBy co_code owner-populated %): clean bimodal split — 36 counties >=93% owner, the 37th drops to 8.9%.
# Route ALL 36 strong-owner counties (incl. every metro) to the proxy. The 23 in-layer withholders (owner stripped) +
# 28 absent counties (51 total) are handled per-county via probes_west. owner_name|owner_more, value emv_total, point-queryable.
MN_GOOD = {"27001","27003","27005","27017","27021","27023","27025","27031","27035","27037","27041","27045",
           "27053","27055","27059","27063","27071","27073","27075","27083","27099","27101","27109","27111",
           "27117","27119","27123","27137","27141","27147","27149","27157","27161","27163","27169","27171"}
route_state("27", "MN", "https://utility.arcgis.com/usrsvcs/servers/1627519e8d3f42bcb55532d48e9a61e5/rest/services/OpenParcels/plan_parcels_open/MapServer/0/query", "owner_name|owner_more", "emv_total", only=MN_GOOD)

# ==== STATEWIDE-SWEEP JACKPOTS (Wave 2, 2026-06-07) — verified live this session. ====
# VT — JACKPOT: VCGI statewide parcels (FeatureServer layer 1), all 14 counties / 256 towns, 94.87% owner
# (326,694/344,361). owner=OWNER1(+OWNER2), value=REAL_FLV, jurisdiction=TOWN. Point-in-polygon at Burlington
# returned SISTERS & BROTHERS INVESTMENT GROUP LLP. VT is town- not county-administered, but one layer = all towns.
route_state("50", "VT", "https://services1.arcgis.com/BkFxaEFNwHqX3tAw/arcgis/rest/services/FS_VCGI_VTPARCELS_WM_NOCACHE_v2/FeatureServer/1/query", "OWNER1|OWNER2", "REAL_FLV")

# WV — JACKPOT: WVU GIS Technical Center statewide parcels. SINGLE MERGED layer 0 (verified live: groupBy
# CountyID returns all 55 codes 01-55 from one layer — NOT sibling layers), 98.78% owner (1,372,844/1,389,855).
# owner=FullOwnerName; no value on layer 0 (TotalAppraisal is in the ParcelSummary join table, layer 11, not
# wired). Exact metro point lands in a road gap -> the resolver's 40m buffer recovers it (900 ON LEE LLC, Kanawha).
route_state("54", "WV", "https://services.wvgis.wvu.edu/arcgis/rest/services/Planning_Cadastre/WV_Parcels/MapServer/0/query", "FullOwnerName", "")

# IA — JACKPOT *with a staleness caveat*: the ONLY statewide owner-populated, point-queryable Iowa layer is the
# 2017 vintage (Iowa Statewide Parcel Data 2017, Univ of Iowa AGOL; live, 2,450,589 parcels, all 99 counties,
# 98.49% DEEDHOLDER-populated; Des Moines point -> KING JAMES L). ~9 yrs stale => owner is wrong wherever a parcel
# transacted since 2017; there is NO current statewide owner FeatureServer (HSEMD ships the current compile as
# downloadable files only). Use as statewide owner backbone / historical baseline; prefer fresh county pulls for
# load-bearing current ownership. owner=DEEDHOLDER (fixed-width LAST/FIRST/MIDDLE; resolver collapses the padding).
# exclude = counties REFRESHED to a current per-county owner layer via probes_west (probe overrides 2017; route_state
# overwrites unconditionally, so without this exclude the 2017 route would clobber the fresher probe). 2026-06-08:
# ia_refresh_sweep found 25 counties with a CURRENT (2020-2026) point-queryable owner layer; ALL 25 independently
# verified point-queryable (21 via parcel round-trip + 4 via resolve.py: Benton->STANDER ROBERT, Des Moines->KNAK
# JANICE K, Dickinson->DICKINSON COUNTY COURTHOUSE SQUARE, Wayne->CORNETT JAMES RUSH). The other 74 IA counties have
# NO current REST owner (Beacon/Schneider/CAMAvision web-apps) -> stay on the labeled 2017 statewide fallback below.
route_state("19", "IA", "https://services3.arcgis.com/kd9gaiUExYqUbnoq/arcgis/rest/services/Iowa_Parcels_2017/FeatureServer/0/query", "DEEDHOLDER", "",
            exclude={"19011","19013","19017","19019","19033","19047","19053","19057","19059","19061",
                     "19067","19077","19113","19127","19129","19131","19133","19139","19155","19163",
                     "19169","19181","19185","19191","19193"},
            note="owner is a 2017 snapshot (only statewide IA owner layer that exists) -- may be stale where the parcel transacted since 2017; verify against a fresh county source for load-bearing current ownership")

# OH — PARTIAL: ODNR odnr_landbase statewide layer (MapServer/4), all 88 counties present, owner populated for 81
# (82.78% overall, OWNER1+OWNER2). 7 counties are owner-stripped at source -> route the 81, mark the 7 unprobed
# (fall back to each county auditor via the layer's AUD_LINK deep-link). value not on this layer.
OH_STRIPPED = {"39045","39053","39113","39139","39153","39105","39095"}  # Fairfield,Gallia,Montgomery(Dayton),Richland,Summit(Akron),Meigs,Lucas(Toledo)
route_state("39", "OH", "https://gis.ohiodnr.gov/arcgis/rest/services/OIT_Services/odnr_landbase/MapServer/4/query", "OWNER1|OWNER2", "", exclude=OH_STRIPPED)
for cf in OH_STRIPPED:
    if cf in rows: continue   # don't clobber a per-county probe (probes_west loaded first) -- 2026-06-08: unprobed_sweep added all 7 OH auditor arcgis layers
    rows[cf] = {"fips":cf,"county":STATE_FIPS["39"].get(cf,""),"state":"OH","method":"unprobed",
                "point_query":"N","owner_fields":"","value_field":"","join_key":"","query_url":""}

# CT — PARTIAL: Connecticut_CAMA_and_Parcel_Layer, statewide, 93.57% owner (1,200,292/1,282,833) across 9 COGs.
# TWO FIPS GOTCHAS handled here:
#  (1) The Census 'Current' geocoder returns the post-2022 PLANNING-REGION county-equivalents (09110-09190),
#      but us_counties.geojson (and route_state) carry the OLD 8 counties (09001-09015). So resolve.py keys on
#      091xx while the coverage map keys on 090xx. We add BOTH row sets (same layer): the 9 regions for the
#      resolver, the 8 old counties (via route_state) for the map.  Region FIPS confirmed live via the geocoder.
#  (2) 7 towns in the Lower CT River Valley region (09130) are owner-suppressed at source (region -> ~45% owner);
#      they fall back to the prior-vintage Connecticut_State_Parcel_Layer_2023 layer (see CT.json), not wired here.
CT_URL = "https://services3.arcgis.com/3FL1kr7L4LvwA2Kb/arcgis/rest/services/Connecticut_CAMA_and_Parcel_Layer/FeatureServer/0/query"
CT_REGIONS = {  # post-2022 planning regions (what the geocoder returns) -> for resolve.py
 "09110":"Capitol","09120":"Greater Bridgeport","09130":"Lower CT River Valley","09140":"Naugatuck Valley",
 "09150":"Northeastern","09160":"Northwest Hills","09170":"South Central","09180":"Southeastern","09190":"Western"}
for fips,name in CT_REGIONS.items():
    rows[fips] = {"fips":fips,"county":name,"state":"CT","method":"arcgis","point_query":"Y",
                  "owner_fields":"Owner|Co_Owner","value_field":"Assessed_Total","join_key":"","query_url":CT_URL}
route_state("09", "CT", CT_URL, "Owner|Co_Owner", "Assessed_Total")  # old 8 counties -> map coloring

# ==== STATEWIDE-SWEEP JACKPOTS (Wave 3, 2026-06-07) — mixed/per-county tier; verified live this session. ====
# TX — JACKPOT: TxGIO StratMap statewide land-parcels compile (all 254 CAD rolls), 97.15% owner. owner=owner_name,
# value=mkt_value. Projected MapServer -> resolver buffers. Blank owner is a SINGLE SPACE (resolver skips blank).
# EXCLUDE Donley (48129, entirely absent from layer) + Terrell (48443, owner stripped) -> per-CAD; mark unprobed.
TX_EXCL = {"48129", "48443"}
route_state("48", "TX", "https://feature.geographic.texas.gov/arcgis/rest/services/Parcels/stratmap_land_parcels_48_most_recent/MapServer/0/query", "owner_name", "mkt_value", exclude=TX_EXCL)
mark_unprobed("48", "TX", TX_EXCL)

# MS — JACKPOT (East/West sibling split): MS_Parcels_August_2024, 99.59% owner. owner=OWNNAME, value=TOTVAL.
# Two sibling layers: West=MapServer/1 (38 counties), East=MapServer/2 (44). Read the FIPS partition from the
# regime file (source of truth); its lists are 3-digit so prefix state 28. Route each county-set to its layer.
_ms = json.load(open(os.path.join(ROOT, "data", "state_owner_regime", "MS.json"), encoding="utf-8"))["sibling_map"]
MS_WEST = {"28" + f for f in _ms["west_county_fips"]}
MS_EAST = {"28" + f for f in _ms["east_county_fips"]}
route_state("28", "MS", _ms["west_layer_url"], "OWNNAME", "TOTVAL", only=MS_WEST)
route_state("28", "MS", _ms["east_layer_url"], "OWNNAME", "TOTVAL", only=MS_EAST)

# NY — PARTIAL: NYS Tax Parcels Public (opt-in per county), single layer, 99.55% owner WITHIN 38 of 62 counties.
# owner=PRIMARY_OWNER|ADD_OWNER, value=FULL_MARKET_VAL. Route the 38 owner-bearing counties; mark the other 24
# unprobed (no data at all in the state layer -> per-county fallback). Re-check annually (NYS adds opt-in counties).
NY_OWNER = {"36001","36005","36007","36011","36013","36023","36029","36037","36039","36041","36047","36049",
 "36051","36057","36061","36065","36067","36069","36071","36075","36077","36079","36081","36083","36085",
 "36087","36089","36097","36101","36103","36105","36107","36109","36111","36113","36117","36119","36121"}
# exclude the 4 counties re-routed to FAST per-county layers via probes_west (2026-06-08): the statewide
# gisservices.its.ny.gov host times out a 60s point query for these big counties; their probes point to fast
# mirrors/self-hosts (Onondaga SOCPA, Westchester+Greene county layers, Ontario via services6 NYS mirror).
NY_SLOW_FIX = {"36039","36067","36069","36119"}
route_state("36", "NY", "https://gisservices.its.ny.gov/arcgis/rest/services/NYS_Tax_Parcels_Public/FeatureServer/1/query", "PRIMARY_OWNER|ADD_OWNER", "FULL_MARKET_VAL", only=NY_OWNER, exclude=NY_SLOW_FIX)
mark_unprobed("36", "NY", [cf for cf in STATE_FIPS.get("36", {}) if cf not in NY_OWNER])

# OK — JACKPOT, but WFS/GeoServer (NOT ArcGIS REST): OGI current_statewide_parcels, 100% owner, all 77 counties.
# owner=owner. method=wfs -> resolver builds a bbox GetFeature (WFS branch). Owner picked nearest-in-bbox (approx).
route_state("40", "OK", "https://okmaps.org/geoserver/wfs?service=WFS&version=2.0.0&request=GetFeature&typeNames=current_statewide_parcels&outputFormat=application/json&count=8", "owner", "", method="wfs",
            note="owner picked nearest-in-bbox via WFS (no exact point-in-polygon on this GeoServer host) -- approx")

# ND — JACKPOT, but owner in a JOIN TABLE (method=arcgis-join): NDGISHUB_Parcels, 98.84% owner across all 53 counties.
# Step 1: point query layer 0 (geometry, has UniqueGISID). Step 2: join table layer 1 on UniqueGISID -> OwnerName.
# value=TotalValue (lives in the table). The polygon layer's own 'Ownership' field is a decoy OWN_TYPE category.
ND_GEOM = "https://services1.arcgis.com/GOcSXpzwBHyk2nog/arcgis/rest/services/NDGISHUB_Parcels/FeatureServer/0/query"
ND_TABLE = "https://services1.arcgis.com/GOcSXpzwBHyk2nog/arcgis/rest/services/NDGISHUB_Parcels/FeatureServer/1/query"
route_state("38", "ND", ND_GEOM, "OwnerName|Owner2Name", "TotalValue", method="arcgis-join", join_key="UniqueGISID", join_url=ND_TABLE)

# ==== STATEWIDE-SWEEP — Wave 4 (2026-06-07): AK/HI/DC — finishes the sweep. ====
# DC — JACKPOT: DCGIS Property_and_Land (one jurisdiction = 11001), 96.77% owner. owner=OWNERNAME, value=ASSESSMENT.
route_state("11", "DC", "https://maps2.dcgis.dc.gov/dcgis/rest/services/DCGIS_DATA/Property_and_Land_WebMercator/FeatureServer/40/query", "OWNERNAME", "ASSESSMENT")

# AK — PARTIAL: SOA-DNR AK_Parcels aggregates 15 organized boroughs into ONE owner-bearing polygon layer (86% overall,
# owner|alt_owner, value total_value). Route the 14 owner-populated boroughs. Juneau (02110, owner 0%) + the ~15
# Unorganized-Borough / census areas (no local assessor / no parcel roll) -> unprobed (genuinely no statewide owner there).
AK_COVERED = {"02020","02170","02122","02090","02150","02130","02185","02195","02100","02220","02068","02060","02275","02282"}
route_state("02", "AK", "https://services1.arcgis.com/7HDiw78fcUiM2BWn/arcgis/rest/services/AK_Parcels/FeatureServer/0/query", "owner|alt_owner", "total_value", only=AK_COVERED)
mark_unprobed("02", "AK", [cf for cf in STATE_FIPS.get("02", {}) if cf not in AK_COVERED])

# HI — PARTIAL: the statewide TMK layer (ParcelsZoning/MapServer/25) is GEOMETRY+TMK only (owner stripped). Only KAUAI
# publishes real per-parcel owner in the state GIS (layer 9, owner 100%). Honolulu/Maui/Hawaii County owner only via each
# county's qPublic/RPAD (web-app). Kalawao (tiny Molokai settlement) has no separate roll -> unprobed.
rows["15007"] = {"fips":"15007","county":"Kauai","state":"HI","method":"arcgis","point_query":"Y",
                 "owner_fields":"owner","value_field":"","join_key":"","note":"","join_url":"",
                 "query_url":"https://geodata.hawaii.gov/arcgis/rest/services/ParcelsZoning/MapServer/9/query"}
for cf, name in {"15003":"Honolulu","15009":"Maui","15001":"Hawaii"}.items():
    rows[cf] = {"fips":cf,"county":name,"state":"HI","method":"web-app-only","point_query":"N",
                "owner_fields":"","value_field":"","join_key":"","query_url":"","join_url":"",
                "note":"owner via county qPublic/RPAD (state GIS strips owner for this island)"}
mark_unprobed("15", "HI", ["15005"])  # Kalawao

# ---- Regional-proxy collapses (2026-06-08, from the MO/IL/KY/VA/NE/AL/LA/PA proxy-spike recon) ----
# VA — HRGeo (Hampton Roads PDC, hosted on HRSD ArcGIS server): ONE token-free layer carries POPULATED owner for 11 of
# the 16 Hampton Roads jurisdictions (the other 5 — Chesapeake/Hampton/Southampton/Surry/Poquoson — strip owner, so they
# are NOT routed here). owner=OWNERNME1, value=TOTVALUE; FIPS field is 3-digit but the resolver point-queries by geometry.
# Verified live 2026-06-08: VaBeach->VIRGINIA HICKORY ASSOCIATES LLP, Norfolk/Newport News real owners, PSTLSTATE=VA.
VA_HRGEO = {"51810","51710","51700","51800","51740","51095","51199","51073","51093","51620","51830"}
route_state("51", "VA", "https://geo.hrsd.com/hrgeo/rest/services/regionalgis/HRGeo_Parcels_Public/MapServer/0/query",
            "OWNERNME1", "TOTVALUE", only=VA_HRGEO, note="Hampton Roads regional parcels (HRGeo); 11 of 16 jurisdictions")

# ME — STATEWIDE JACKPOT (arcgis-join). MEGIS "Maine Parcels Organized Towns": geometry layer 10 (STATE_ID, no owner)
# joins to the ADB table 9 (STATE_ID -> OWNER1/OWNER2 + LAND_VAL/BLDG_VAL). 88% owner-populated statewide (459,790 parcels),
# covers all 16 counties' ORGANIZED towns (unorganized territories not in the layer). Verified: Portland->CUMBERLAND COUNTY OF,
# Augusta->DUMOND JENNIFER, Bangor->FITZGERALD. Routes all 16 ME counties; overrides the per-county single-town probes.
_ME = "https://services1.arcgis.com/RbMX0mRVOFNTdLzd/arcgis/rest/services/Maine_Parcels_Organized_Towns/FeatureServer"
route_state("23", "ME", _ME + "/10/query", "OWNER1|OWNER2", "", method="arcgis-join",
            join_key="STATE_ID", join_url=_ME + "/9/query",
            note="Statewide MEGIS organized-towns parcels; owner via ADB table join on STATE_ID (88% statewide; unorganized territories not covered)")

# NH — STATEWIDE JACKPOT (direct arcgis). NHGRANIT 'CAD_ParcelMosaicSecure' layer 7 (services9.arcgis.com/wnvDDrXX8EouLkZP)
# carries OwnerName 97.5% statewide (600,828 parcels) + TaxTotal, anonymous/public. (Distinct from the OWNER-STRIPPED public
# nhgeodata.unh.edu Hosted/CAD_ParcelMosaic where name=CamaID.) Covers all 10 NH counties; verified Concord->NH HISTORICAL
# SOCIETY, Manchester->74-78 WALNUT STREET LLC, Portsmouth->WU JONATHAN. Overrides the agents' single-town partials.
route_state("33", "NH", "https://services9.arcgis.com/wnvDDrXX8EouLkZP/arcgis/rest/services/CAD_ParcelMosaicSecure/FeatureServer/7/query",
            "OwnerName|CoOwnerName", "TaxTotal", note="Statewide NHGRANIT CAD_ParcelMosaicSecure; OwnerName 97.5% statewide, all 10 counties")

# PR — STATEWIDE JACKPOT (all 78 municipios). The official CRIM cadastre (crimpr.net) owner is token-gated (499), BUT a full CRIM
# tax-roll snapshot is republished public by Para La Naturaleza (NGO): gis.paralanaturaleza.org/.../catastro_2018_05_17 — owner field
# `contact` 100% populated (1,636,107 parcels), value=totalval, covers all 78 municipios. CAVEAT: 2018 vintage (~stale, like IA-2017).
# Verified: Ponce->PONCE BANK INC, Mayaguez->BANCO POPULAR, Caguas->BORROTO PEREZ ABEL. Overrides the agents' 9 arcgis + 69 web-app-only.
route_state("72", "PR", "https://gis.paralanaturaleza.org/server/rest/services/Hosted/catastro_2018_05_17/FeatureServer/0/query",
            "contact", "totalval", note="Statewide PR CRIM tax-roll 2018 SNAPSHOT (Para La Naturaleza host); owner=contact 100% populated, all 78 municipios; ~2018 vintage (stale)")

# ---- complete the CA crosswalk: mark the 58 CA counties not individually probed as
# 'unprobed' (NOT presumed-suppressed: rural CA is mixed — ~3/8 spot-checks exposed owner). ----
CA_ALL = {
 "06001":"Alameda","06003":"Alpine","06005":"Amador","06007":"Butte","06009":"Calaveras",
 "06011":"Colusa","06013":"Contra Costa","06015":"Del Norte","06017":"El Dorado","06019":"Fresno",
 "06021":"Glenn","06023":"Humboldt","06025":"Imperial","06027":"Inyo","06029":"Kern","06031":"Kings",
 "06033":"Lake","06035":"Lassen","06037":"Los Angeles","06039":"Madera","06041":"Marin",
 "06043":"Mariposa","06045":"Mendocino","06047":"Merced","06049":"Modoc","06051":"Mono",
 "06053":"Monterey","06055":"Napa","06057":"Nevada","06059":"Orange","06061":"Placer","06063":"Plumas",
 "06065":"Riverside","06067":"Sacramento","06069":"San Benito","06071":"San Bernardino","06073":"San Diego",
 "06075":"San Francisco","06077":"San Joaquin","06079":"San Luis Obispo","06081":"San Mateo",
 "06083":"Santa Barbara","06085":"Santa Clara","06087":"Santa Cruz","06089":"Shasta","06091":"Sierra",
 "06093":"Siskiyou","06095":"Solano","06097":"Sonoma","06099":"Stanislaus","06101":"Sutter",
 "06103":"Tehama","06105":"Trinity","06107":"Tulare","06109":"Tuolumne","06111":"Ventura",
 "06113":"Yolo","06115":"Yuba"}
for fips, name in CA_ALL.items():
    if fips not in rows:
        rows[fips] = {"fips": fips, "county": name, "state": "CA", "method": "unprobed",
                      "point_query": "N", "owner_fields": "", "query_url": "",
                      "value_field": "", "join_key": ""}

# ---- NOTE_OVERRIDES: per-county caveats from the OWNER-CORRECTNESS / JOIN-WFS audits (2026-06-09).
# Applied LAST so route_state()'s unconditional rows[cf]=... (no in-rows guard) can't clobber them.
# These are owner-QUALITY findings the probe/route inputs don't carry; add new audit caveats here. ----
NOTE_OVERRIDES = {
    "53075": "COVERAGE CAVEAT (owner-quality audit 2026-06-09): Last_Name is only ~3.4% populated (1,186/35,121) and is LAST-NAME-ONLY -- this layer is a parcel fabric, not a full owner roll, so the resolver returns an owner for only a small minority of Whitman parcels (most return blank). A better full-name Whitman owner source was not located. Best-available but LOW-COVERAGE; do not treat a blank result here as 'no owner exists'.",
    "23003": "OWNER-GAP (join/wfs audit 2026-06-09): geometry resolves, but the MEGIS ADB owner table (FeatureServer/9) has owner data for 0/22 organized towns in Aroostook -- the STATE_IDs do not join, so the resolver returns no owner for the ENTIRE county. Effectively NOT owner-resolvable despite pq=Y; geometry+location only. Candidate for a dedicated Aroostook owner source (county/town self-host) -- the statewide ADB does not cover it. Do NOT count as owner-resolvable.",
    "23017": "PARTIAL owner coverage (join/wfs audit 2026-06-09): MEGIS ADB owner table covers 14/25 organized towns in Oxford (~56%); resolver returns owner for covered towns, no-owner for the other 11. Resolvable but town-dependent. (Statewide MEGIS organized-towns parcels; owner via ADB table join on STATE_ID; 88% statewide; unorganized territories not covered.)",
    "53031": "OWNER PLACEHOLDER CAVEAT (dataset full-distribution audit 2026-06-10): Jefferson WA's Owner_Name masks NATURAL PERSONS as the literal string 'INDIVIDUAL OWNER' (25,965/32,733 parcels = 79%); only entity/government parcels carry a real name. Point-sampling missed this because the field IS populated -- the bulk-ingest distribution analysis caught it. Treat 'INDIVIDUAL OWNER' as owner-suppressed, not as an owner name.",
}
for _fp, _nt in NOTE_OVERRIDES.items():
    if _fp in rows: rows[_fp]["note"] = _nt

rows = sorted(rows.values(), key=lambda r:(r["state"],r["county"]))
with open(OUT,"w",newline="",encoding="utf-8") as f:
    w=csv.DictWriter(f,fieldnames=["fips","county","state","method","point_query","owner_fields","value_field","join_key","query_url","note","join_url"],
                     restval="", extrasaction="ignore")
    w.writeheader(); w.writerows(rows)

from collections import Counter
print(f"wrote {len(rows)} counties -> {OUT}")
for st,c in Counter(r["state"] for r in rows).most_common():
    print(f"  {st}: {c} counties")
print("methods:")
for m,c in Counter(r["method"] for r in rows).most_common():
    print(f"  {m:<14} {c}")
print(f"  resolver-ready now (method=arcgis): {sum(1 for r in rows if r['method']=='arcgis')}")
resolvable = sum(1 for r in rows if r['method'] in ("arcgis", "wfs") or (r['method'] == "arcgis-join" and r.get("join_url")))
print(f"  RESOLVABLE total (arcgis + wfs + arcgis-join-with-table): {resolvable}")
print(f"  point-queryable total: {sum(1 for r in rows if r['point_query']=='Y')}")
