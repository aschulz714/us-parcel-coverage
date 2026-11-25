"""King + Pierce WA — bulk-join ingesters (the counties live REST can't serve owners for).

Both counties strip owner names from their REST/GIS layers (RCW 42.56.070), but both
publish FREE bulk extracts that carry the data another way:

  KING 53033  geometry: gismaps.kingcounty.gov Property/KingCo_Parcels (PIN only)
              owner:    EXTR_RPSale.csv -> most-recent deed GRANTEE per parcel (the proven
                        King County reconstruction; "Real Property Sales.zip")
              values:   EXTR_RPAcct_NoName.csv (ApprLandVal+ApprImpsVal, BillYr, mailing)
              land use: EXTR_Parcel.csv (PresentUse, SqFtLot -> acreage)
  PIERCE 53053 geometry+situs+values+use: services2 Tax_Parcels open-data layer
              owner:    datamart sale.txt -> most-recent GRANTEE per parcel

HONESTY CAVEATS (carried into the manifest):
  - Owner-by-deed covers only parcels that TRANSACTED within the extract history; never-sold
    parcels get owner=None. Coverage is measured and reported, never assumed.
  - Grantee = owner-of-record as of the last recorded conveyance.
  - King's extract page carries an RCW 42.56.070(8) no-commercial-purpose notice for name
    lists - fine for research, review before any commercial/published use.

Source zips are expected in data/parcels/bulk_src/ (king_rpsale.zip, king_rpacct.zip,
king_parcel.zip, pierce_sale.zip) - download URLs in NOTES below. Geometry is pulled live.

Usage:
  python src/ingest_bulk_wa.py --county king
  python src/ingest_bulk_wa.py --county pierce
  python src/ingest_bulk_wa.py --county king --geometry-only   # just the REST pull
"""
import argparse, datetime, io, json, os, sys, zipfile

import geopandas as gpd
import pandas as pd

sys.path.insert(0, os.path.dirname(__file__))
import ingest  # reuse the hardened REST extractor (pagination, fallbacks, dtype unification)
from atomic_io import atomic_replace_writer

HERE = os.path.dirname(__file__)
ROOT = os.path.dirname(HERE)
PARCELS = os.path.join(ROOT, "data", "parcels")
BULK = os.path.join(PARCELS, "bulk_src")
RAW = os.path.join(PARCELS, "raw")
NORM = os.path.join(PARCELS, "normalized")
MANIFEST = os.path.join(PARCELS, "manifest.json")

NOTES = {
    "53033": "BULK-JOIN: owner = most-recent deed grantee (EXTR_RPSale, aqua.kingcounty.gov/extranet/assessor/); "
             "values/mailing = EXTR_RPAcct_NoName (BillYr roll); use/acreage = EXTR_Parcel; geometry = KingCo_Parcels REST. "
             "Never-sold parcels have owner=None. RCW 42.56.070(8): name list not for commercial purposes.",
    "53053": "BULK-JOIN: owner = most-recent grantee (online.co.pierce.wa.us/datamart/sale.zip); geometry/situs/values/use = "
             "Tax_Parcels open-data layer. Never-sold parcels have owner=None.",
    "53011": "OWNER SUPPRESSED COUNTY-WIDE: Clark redacts owner names from ALL public GIS data per RCW 42.56.070(8); "
             "owner only via records request to the Treasurer (treasoff@clark.wa.gov). Geometry/situs/values/use ARE "
             "public (TaxlotsPublic). The dataset carries the explicit sentinel '" + "[SUPPRESSED" + "...' in owner so "
             "Clark parcels are present and searchable by address/APN/value; research.py excludes sentinels from owner rankings.",
    "53027": "BULK-FILE COUNTY: Grays Harbor publishes no parcel REST; its own File-GDB download "
             "(graysharbor.us/GIS/Data_Download/Parcel.gdb.zip, updated monthly) carries OWNER + situs + values + APR_YEAR. "
             "The only free owner source (statewide layer strips owner).",
    "53049": "BULK-FILE COUNTY: Pacific publishes no parcel REST; its shapefile download "
             "(co.pacific.wa.us .../tax_lots.zip, WA State Plane S EPSG:2927) carries CURRENT_O owner + "
             "SITUS + single combined ASSESSED value. The only free owner source.",
    "STATEWIDE_BACKFILL": "STATEWIDE-LAYER BACKFILL: county has NO free owner source anywhere (Spatialest "
                          "TaxSifter/MapSifter web apps only — original F grade). Geometry/situs/values/land-use "
                          "come from WaTech's statewide Parcels_2026 layer (owner stripped statewide); owner is the "
                          "explicit NOT-PUBLISHED sentinel. Owner obtainable only via an assessor records request.",
}

NOT_PUBLISHED_SENTINEL = "[NOT PUBLISHED - no free owner source; via county assessor records request]"

# WaTech statewide layer (owner-stripped; per-county slice via where=FIPS_NR='XXX')
STATEWIDE_URL = ("https://services.arcgis.com/jsIt88o09Q0r1j8h/arcgis/rest/services/"
                 "Current_Parcels/FeatureServer/0/query")

# Owner sentinel for counties whose owner is lawfully redacted: bracketed so it can never be
# mistaken for a real name, and excludable with owner NOT LIKE '[%'.
CLARK_SENTINEL = "[SUPPRESSED - RCW 42.56.070(8); request via Clark Co Treasurer]"

# The bulk source files each county's build step joins against (cached in data/parcels/bulk_src/).
# refresh.py HEADs these for Last-Modified staleness and re-downloads them on refresh.
BULK_SOURCES = {
    "king": {
        "king_rpsale.zip": "https://aqua.kingcounty.gov/extranet/assessor/Real%20Property%20Sales.zip",
        "king_rpacct.zip": "https://aqua.kingcounty.gov/extranet/assessor/Real%20Property%20Account.zip",
        "king_parcel.zip": "https://aqua.kingcounty.gov/extranet/assessor/Parcel.zip",
    },
    "pierce": {"pierce_sale.zip": "https://online.co.pierce.wa.us/datamart/sale.zip"},
    "clark": {},  # REST-only; no bulk files
    # moved 2026-08 (the old /Parcel.gdb.zip now answers HTTP 200 with an HTML "404" page)
    "grays_harbor": {"gh_parcel_gdb.zip": "https://www.graysharbor.us/GIS/Data_Download/Parcel.gdb.zip"},
    "pacific": {"pacific_taxlots.zip":
                "https://www.co.pacific.wa.us/gis/desktopgis/web/data_2006/tax_lots.zip"},
    "adams": {}, "garfield": {}, "lincoln": {},  # statewide-layer REST only
}


def download_sources(county, force=False):
    import requests
    os.makedirs(BULK, exist_ok=True)
    for fname, url in BULK_SOURCES[county].items():
        path = os.path.join(BULK, fname)
        if os.path.exists(path) and not force:
            continue
        print(f"  downloading {fname} ...", flush=True)
        r = requests.get(url, headers={"User-Agent": ingest.UA["User-Agent"]}, timeout=600)
        r.raise_for_status()
        check_download(fname, url, r.content, r.headers.get("Content-Type", ""))
        def _write(temp, body=r.content):
            with open(temp, "wb") as f:
                f.write(body)
        # atomic: a failed/partial download never replaces the last good extract
        atomic_replace_writer(path, _write)


def check_download(fname, url, content, content_type=""):
    """Refuse a body that is not the archive we asked for. Moved county files often answer
    HTTP 200 with an HTML error page (Grays Harbor, 2026-08) — writing that over the cached
    zip destroyed the last good extract and the builder then failed on it."""
    if fname.lower().endswith(".zip") and not content.startswith(b"PK\x03\x04"):
        head = content[:80].decode("latin-1", "replace").strip()
        raise RuntimeError(f"{url} returned {len(content):,} bytes of {content_type or 'unknown type'} "
                           f"that is not a zip (starts {head!r}); source moved? cached copy kept")

GEOM_ROWS = {
    "king": {"fips": "53033", "county": "King", "state": "WA", "owner_fields": "", "value_field": "",
             "note": NOTES["53033"],
             "query_url": "https://gismaps.kingcounty.gov/arcgis/rest/services/Property/KingCo_Parcels/MapServer/0/query"},
    "pierce": {"fips": "53053", "county": "Pierce", "state": "WA", "owner_fields": "", "value_field": "",
               "note": NOTES["53053"],
               "query_url": "https://services2.arcgis.com/1UvBaQ5y1ubjUPmd/arcgis/rest/services/Tax_Parcels/FeatureServer/0/query"},
    "clark": {"fips": "53011", "county": "Clark", "state": "WA", "owner_fields": "", "value_field": "",
              "note": NOTES["53011"],
              "query_url": "https://gis.clark.wa.gov/arcgisfed/rest/services/ClarkView_Public/TaxlotsPublic/MapServer/0/query"},
    "adams": {"fips": "53001", "county": "Adams", "state": "WA", "owner_fields": "", "value_field": "",
              "note": NOTES["STATEWIDE_BACKFILL"], "query_url": STATEWIDE_URL, "where": "FIPS_NR='001'"},
    "garfield": {"fips": "53023", "county": "Garfield", "state": "WA", "owner_fields": "", "value_field": "",
                 "note": NOTES["STATEWIDE_BACKFILL"], "query_url": STATEWIDE_URL, "where": "FIPS_NR='023'"},
    "lincoln": {"fips": "53043", "county": "Lincoln", "state": "WA", "owner_fields": "", "value_field": "",
                "note": NOTES["STATEWIDE_BACKFILL"], "query_url": STATEWIDE_URL, "where": "FIPS_NR='043'"},
    # Oregon owner-stripped counties (Clark-class sentinels): geometry/situs/values public
    "clackamas": {"fips": "41005", "county": "Clackamas", "state": "OR", "owner_fields": "", "value_field": "",
                  "note": "OWNER-STRIPPED public view layer (county AGOL; source schema holds taxpayer/value but the "
                          "public view exposes parcel/TLNO/situs only). Sentinel owner; records request for names.",
                  "query_url": "https://services3.arcgis.com/I2eWXOndpF9m8oKC/arcgis/rest/services/Taxlots/FeatureServer/0/query"},
    "tillamook": {"fips": "41057", "county": "Tillamook", "state": "OR", "owner_fields": "", "value_field": "",
                  "note": "OWNER-STRIPPED county AGOL Taxlots (mailing address present, no owner name). Sentinel owner.",
                  "query_url": "https://services3.arcgis.com/JnnlTIrcZLzs7d68/arcgis/rest/services/Taxlots/FeatureServer/0/query"},
    "morrow": {"fips": "41049", "county": "Morrow", "state": "OR", "owner_fields": "", "value_field": "",
               "note": "County's own apps use the explicitly named Taxlots_wo_Ownership service. Sentinel owner.",
               "query_url": "https://services6.arcgis.com/MQGnkVpUbcAxMorv/arcgis/rest/services/mc_taxlots/FeatureServer/0/query"},
}

SCHEMA = ["state", "county_fips", "county_name", "apn", "owner", "situs_address",
          "mailing_address", "assessed_value", "land_use", "acreage",
          "source_url", "source_vintage", "retrieved_at", "geometry"]


def _s(series):
    """Nullable column -> clean string series. fillna BEFORE astype(str), else pandas turns
    missing values into the literal strings '<NA>'/'None' (the Lincoln apn bug)."""
    return series.fillna("").astype(str).str.strip()


def _zip_csv(zname, fname, **kw):
    with zipfile.ZipFile(os.path.join(BULK, zname)).open(fname) as fh:
        return pd.read_csv(io.BytesIO(fh.read()), encoding="latin-1", dtype=str, **kw)


def _latest_grantee(df, key, date_col, grantee_col):
    """Most-recent conveyance per parcel -> owner-of-record by deed."""
    df = df.dropna(subset=[key, grantee_col])
    df = df[df[grantee_col].str.strip() != ""]
    d = pd.to_datetime(df[date_col], format="%m/%d/%Y", errors="coerce")
    df = df.assign(_d=d).dropna(subset=["_d"]).sort_values("_d")
    last = df.groupby(key).tail(1)
    return last.set_index(key)[grantee_col].str.strip().str.split().str.join(" ")


def _save(gdf, fips, slug, entry_extra):
    # Degenerate geometries (the statewide layer ships a few WORLD-SPANNING rings) sit
    # invisibly over the whole map and steal clicks everywhere — drop them. 5 degrees is
    # far beyond any real record (the biggest legit aggregate, Jefferson WATERWAYS, is 1.8).
    b = gdf.geometry.bounds
    junk = gdf.geometry.notna() & (((b.maxx - b.minx) > 5) | ((b.maxy - b.miny) > 5))
    if junk.any():
        print(f"  dropped {int(junk.sum())} degenerate world-spanning geometries")
        gdf = gdf[~junk]
    out_path = os.path.join(NORM, f"{fips}_{slug}.parquet")
    os.makedirs(NORM, exist_ok=True)
    gdf[SCHEMA].to_parquet(out_path)
    with open(MANIFEST, encoding="utf-8") as f:
        manifest = json.load(f)
    pop = {c: round(float(gdf[c].notna().mean()), 3) for c in
           ("owner", "apn", "situs_address", "mailing_address", "assessed_value")}
    manifest.setdefault(fips, {}).update({
        "normalized_file": os.path.relpath(out_path, ROOT).replace("\\", "/"),
        "feature_count": int(len(gdf)), "populated": pop, **entry_extra})
    with open(MANIFEST, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=1, sort_keys=True)
    print(f"  DONE {fips}: {len(gdf):,} parcels -> {out_path}")
    print(f"  populated: " + " ".join(f"{k} {v:.0%}" for k, v in pop.items()))


def pull_geometry(county, refresh=False):
    if county not in GEOM_ROWS:
        return  # file-only county (Grays Harbor / Pacific): geometry comes from the bulk zip
    row = GEOM_ROWS[county]
    entry = ingest.ingest_county(row, RAW, refresh=refresh)
    if entry:
        manifest = ingest.load_manifest(MANIFEST)
        manifest.setdefault(row["fips"], {}).update(entry)
        with open(MANIFEST, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=1, sort_keys=True)


def build_king():
    geom = gpd.read_parquet(os.path.join(RAW, "53033_king.parquet"))
    geom["PIN"] = _s(geom["PIN"])
    print(f"  geometry: {len(geom):,} parcels")

    sale = _zip_csv("king_rpsale.zip", "EXTR_RPSale.csv",
                    usecols=["Major", "Minor", "DocumentDate", "BuyerName"])
    sale["PIN"] = sale["Major"].str.zfill(6) + sale["Minor"].str.zfill(4)
    owner = _latest_grantee(sale, "PIN", "DocumentDate", "BuyerName")
    print(f"  deed grantees: {len(owner):,} parcels with a recorded sale (of {len(sale):,} sale rows)")

    acct = _zip_csv("king_rpacct.zip", "EXTR_RPAcct_NoName.csv")
    acct["PIN"] = acct["Major"].str.zfill(6) + acct["Minor"].str.zfill(4)
    for c in ("ApprLandVal", "ApprImpsVal"):
        acct[c] = pd.to_numeric(acct[c], errors="coerce")
    acct["BillYr"] = pd.to_numeric(acct["BillYr"], errors="coerce")
    acct = acct.sort_values("BillYr").groupby("PIN").tail(1).set_index("PIN")
    bill_yr = int(acct["BillYr"].max())
    mailing = (acct["AddrLine"].fillna("").str.strip() + ", " + acct["CityState"].fillna("").str.strip()
               + " " + acct["ZipCode"].fillna("").str.strip()).str.strip(", ").str.split().str.join(" ")
    value = acct["ApprLandVal"].fillna(0) + acct["ApprImpsVal"].fillna(0)

    par = _zip_csv("king_parcel.zip", "EXTR_Parcel.csv", usecols=["Major", "Minor", "PresentUse", "SqFtLot"])
    par["PIN"] = par["Major"].str.zfill(6) + par["Minor"].str.zfill(4)
    par = par.groupby("PIN").first()

    out = gpd.GeoDataFrame(geometry=geom.geometry, crs=geom.crs)
    out["state"], out["county_fips"], out["county_name"] = "WA", "53033", "King"
    out["apn"] = geom["PIN"]
    out["owner"] = geom["PIN"].map(owner)
    out["situs_address"] = None  # not in the parcel layer or extracts; KCGIS address layer = future join
    out["mailing_address"] = geom["PIN"].map(mailing).replace("", None)
    out["assessed_value"] = geom["PIN"].map(value).replace(0, None)
    out["land_use"] = geom["PIN"].map(par["PresentUse"])
    out["acreage"] = (pd.to_numeric(geom["PIN"].map(par["SqFtLot"]), errors="coerce") / 43560).round(4)
    out["source_url"] = GEOM_ROWS["king"]["query_url"]
    out["source_vintage"] = f"{bill_yr} roll + deeds"
    out["retrieved_at"] = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    _save(out, "53033", "king", {
        "county": "King", "state": "WA", "note": NOTES["53033"],
        "source_url": GEOM_ROWS["king"]["query_url"], "source_last_edit": None,
        "owner_fields": "EXTR_RPSale latest BuyerName", "value_field": "ApprLandVal+ApprImpsVal",
        "retrieved_at": out["retrieved_at"].iloc[0], "source_vintage": f"{bill_yr} roll + deeds",
        "field_map": {"owner": "deed-grantee(EXTR_RPSale)", "assessed_value": "ApprLandVal+ApprImpsVal",
                      "apn": "PIN", "mailing_address": "AddrLine+CityState+ZipCode",
                      "land_use": "PresentUse", "acreage": "SqFtLot/43560"}})


PIERCE_SALE_COLS = ["ETN", "ParcelCount", "ParcelNumber", "SaleDate", "SalePrice", "DeedType",
                    "Grantor", "Grantee", "Valid", "Confirmed", "ExcludeReason", "ImprovedVacant",
                    "ApprAcctType"]


def build_pierce():
    geom = gpd.read_parquet(os.path.join(RAW, "53053_pierce.parquet"))
    if "RetiredDate" in geom.columns:
        live = geom["RetiredDate"].isna()
        print(f"  geometry: {len(geom):,} rows, {live.sum():,} non-retired kept")
        geom = geom[live]
    geom["TaxParcelNumber"] = _s(geom["TaxParcelNumber"])
    geom = geom.drop_duplicates(subset=["TaxParcelNumber"])

    with zipfile.ZipFile(os.path.join(BULK, "pierce_sale.zip")).open("sale.txt") as fh:
        sale = pd.read_csv(io.BytesIO(fh.read()), sep="|", names=PIERCE_SALE_COLS,
                           dtype=str, encoding="latin-1", header=None)
    owner = _latest_grantee(sale, "ParcelNumber", "SaleDate", "Grantee")
    print(f"  grantees: {len(owner):,} parcels with a recorded sale (of {len(sale):,} sale rows)")

    out = gpd.GeoDataFrame(geometry=geom.geometry, crs=geom.crs)
    out["state"], out["county_fips"], out["county_name"] = "WA", "53053", "Pierce"
    out["apn"] = geom["TaxParcelNumber"]
    out["owner"] = geom["TaxParcelNumber"].map(owner)
    out["situs_address"] = geom["Site_Address"].fillna("").str.split().str.join(" ").replace("", None)
    out["mailing_address"] = (geom["Delivery_Address"].fillna("").str.strip() + ", "
                              + geom["City_State"].fillna("").str.strip() + " "
                              + geom["Zipcode"].fillna("").str.strip()).str.strip(", ").str.split().str.join(" ").replace("", None)
    out["assessed_value"] = (pd.to_numeric(geom["Land_Value"], errors="coerce").fillna(0)
                             + pd.to_numeric(geom["Improvement_Value"], errors="coerce").fillna(0)).replace(0, None)
    out["land_use"] = geom["Landuse_Description"]
    out["acreage"] = pd.to_numeric(geom["Land_Acres"], errors="coerce")
    out["source_url"] = GEOM_ROWS["pierce"]["query_url"]
    edit = pd.to_datetime(geom.get("EditDate"), errors="coerce").max()
    out["source_vintage"] = (str(edit.date()) if pd.notna(edit) else None)
    out["retrieved_at"] = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    _save(out, "53053", "pierce", {
        "county": "Pierce", "state": "WA", "note": NOTES["53053"],
        "source_url": GEOM_ROWS["pierce"]["query_url"], "source_last_edit": out["source_vintage"].iloc[0],
        "owner_fields": "datamart sale.txt latest Grantee", "value_field": "Land_Value+Improvement_Value",
        "retrieved_at": out["retrieved_at"].iloc[0], "source_vintage": out["source_vintage"].iloc[0],
        "field_map": {"owner": "deed-grantee(sale.txt)", "assessed_value": "Land_Value+Improvement_Value",
                      "apn": "TaxParcelNumber", "situs_address": "Site_Address",
                      "mailing_address": "Delivery_Address+City_State+Zipcode",
                      "land_use": "Landuse_Description", "acreage": "Land_Acres"}})


def build_clark():
    geom = gpd.read_parquet(os.path.join(RAW, "53011_clark.parquet"))
    geom["Prop_id"] = _s(geom["Prop_id"])
    print(f"  geometry: {len(geom):,} taxlots")

    out = gpd.GeoDataFrame(geometry=geom.geometry, crs=geom.crs)
    out["state"], out["county_fips"], out["county_name"] = "WA", "53011", "Clark"
    out["apn"] = geom["Prop_id"].replace("", None)
    out["owner"] = CLARK_SENTINEL  # county-wide lawful redaction; see NOTES['53011']
    out["situs_address"] = geom["SitusAddrsFull"].fillna("").str.split().str.join(" ").replace("", None)
    out["mailing_address"] = None  # owner-related info is what RCW strips
    out["assessed_value"] = pd.to_numeric(geom["MktTotVal"], errors="coerce").replace(0, None)
    out["land_use"] = geom["Pt1Desc"]
    out["acreage"] = pd.to_numeric(geom["GISAc"], errors="coerce")
    out["source_url"] = GEOM_ROWS["clark"]["query_url"]
    vintage = pd.to_numeric(geom.get("AvYear"), errors="coerce").max()
    out["source_vintage"] = (f"AV {int(vintage)}" if pd.notna(vintage) else None)
    out["retrieved_at"] = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    _save(out, "53011", "clark", {
        "county": "Clark", "state": "WA", "note": NOTES["53011"],
        "source_url": GEOM_ROWS["clark"]["query_url"], "source_last_edit": out["source_vintage"].iloc[0],
        "owner_fields": "SENTINEL (county-wide redaction)", "value_field": "MktTotVal",
        "retrieved_at": out["retrieved_at"].iloc[0], "source_vintage": out["source_vintage"].iloc[0],
        "field_map": {"owner": "sentinel:[SUPPRESSED-RCW]", "assessed_value": "MktTotVal",
                      "apn": "Prop_id", "situs_address": "SitusAddrsFull",
                      "land_use": "Pt1Desc", "acreage": "GISAc"}})


def _now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_grays_harbor():
    gdf = gpd.read_file("/vsizip/" + os.path.join(BULK, "gh_parcel_gdb.zip").replace("\\", "/"),
                        layer="Parcel_poly").to_crs(4326)
    print(f"  file-GDB: {len(gdf):,} parcels")
    out = gpd.GeoDataFrame(geometry=gdf.geometry, crs=gdf.crs)
    out["state"], out["county_fips"], out["county_name"] = "WA", "53027", "Grays Harbor"
    out["apn"] = _s(gdf["PARCELATT"]).replace("", None)
    out["owner"] = gdf["OWNER"].fillna("").str.split().str.join(" ").replace("", None)
    out["situs_address"] = gdf["SITUS"].fillna("").str.split().str.join(" ").replace("", None)
    out["mailing_address"] = (gdf["ADDRESS"].fillna("").str.strip() + ", " + gdf["CITY"].fillna("").str.strip()
                              + " " + gdf["STATE"].fillna("").str.strip() + " " + _s(gdf["ZIPCODE"])
                              ).str.strip(", ").str.split().str.join(" ").replace("", None)
    out["assessed_value"] = pd.to_numeric(gdf["TOTAPR"], errors="coerce").replace(0, None)
    out["land_use"] = gdf["LANDUSE"]
    out["acreage"] = pd.to_numeric(gdf["TOTACRES"], errors="coerce")
    out["source_url"] = BULK_SOURCES["grays_harbor"]["gh_parcel_gdb.zip"]
    vintage = pd.to_numeric(gdf["APR_YEAR"], errors="coerce").max()
    out["source_vintage"] = f"AV {int(vintage)}" if pd.notna(vintage) else None
    out["retrieved_at"] = _now_iso()
    _save(out, "53027", "grays_harbor", {
        "county": "Grays Harbor", "state": "WA", "note": NOTES["53027"],
        "source_url": out["source_url"].iloc[0], "source_last_edit": out["source_vintage"].iloc[0],
        "owner_fields": "OWNER (county File-GDB)", "value_field": "TOTAPR",
        "retrieved_at": out["retrieved_at"].iloc[0], "source_vintage": out["source_vintage"].iloc[0],
        "field_map": {"owner": "OWNER", "assessed_value": "TOTAPR", "apn": "PARCELATT",
                      "situs_address": "SITUS", "mailing_address": "ADDRESS+CITY+STATE+ZIPCODE",
                      "land_use": "LANDUSE", "acreage": "TOTACRES"}})


def build_pacific():
    gdf = gpd.read_file("/vsizip/" + os.path.join(BULK, "pacific_taxlots.zip").replace("\\", "/"))
    if gdf.crs is None:
        gdf = gdf.set_crs(2927)  # WA State Plane South ftUS, per the original county probe
    gdf = gdf.to_crs(4326)
    print(f"  shapefile: {len(gdf):,} taxlots")
    out = gpd.GeoDataFrame(geometry=gdf.geometry, crs=gdf.crs)
    out["state"], out["county_fips"], out["county_name"] = "WA", "53049", "Pacific"
    out["apn"] = _s(gdf["ASSESSOR"]).replace("", None)
    out["owner"] = (gdf["CURRENT_O"].fillna("").str.split().str.join(" ")
                    .str.rstrip(",").replace("", None))
    out["situs_address"] = gdf["SITUS"].fillna("").str.split().str.join(" ").replace("", None)
    out["mailing_address"] = (gdf["MAIL_ADD"].fillna("").str.strip() + " " + gdf["MAIL_ADD1"].fillna("").str.strip()
                              + " " + gdf["MAIL_ADD2"].fillna("").str.strip()
                              ).str.split().str.join(" ").replace("", None)
    out["assessed_value"] = pd.to_numeric(gdf["ASSESSED"], errors="coerce").replace(0, None)
    out["land_use"] = None  # no use-code field in the shapefile
    out["acreage"] = pd.to_numeric(gdf["ACRES"], errors="coerce")
    out["source_url"] = BULK_SOURCES["pacific"]["pacific_taxlots.zip"]
    out["source_vintage"] = None  # file Last-Modified is the currency signal (refresh.py HEADs it)
    out["retrieved_at"] = _now_iso()
    _save(out, "53049", "pacific", {
        "county": "Pacific", "state": "WA", "note": NOTES["53049"],
        "source_url": out["source_url"].iloc[0], "source_last_edit": None,
        "owner_fields": "CURRENT_O (county shapefile)", "value_field": "ASSESSED",
        "retrieved_at": out["retrieved_at"].iloc[0], "source_vintage": None,
        "field_map": {"owner": "CURRENT_O", "assessed_value": "ASSESSED", "apn": "ASSESSOR",
                      "situs_address": "SITUS", "mailing_address": "MAIL_ADD+MAIL_ADD1+MAIL_ADD2",
                      "acreage": "ACRES"}})


OWNER_STRIPPED_SENTINEL = "[SUPPRESSED - owner stripped from county GIS; via county assessor records request]"


def _maybe(gdf, *cands):
    """First candidate column that exists, else None — schema-tolerant field lookup."""
    return next((c for c in cands if c in gdf.columns), None)


def _build_or_sentinel(county, fips, name, apn_cands, situs_cands, situs_city_cands,
                       mail_cands, acre_cands):
    """Owner-stripped OR counties (Clackamas/Tillamook/Morrow): everything public except
    the owner name — ingest with the explicit sentinel, like Clark WA."""
    import glob as _glob
    gdf = gpd.read_parquet(_glob.glob(os.path.join(RAW, f"{fips}_*.parquet"))[0])
    print(f"  geometry: {len(gdf):,} taxlots")
    out = gpd.GeoDataFrame(geometry=gdf.geometry, crs=gdf.crs)
    out["state"], out["county_fips"], out["county_name"] = "OR", fips, name
    apn = _maybe(gdf, *apn_cands)
    out["apn"] = _s(gdf[apn]).replace("", None) if apn else None
    out["owner"] = OWNER_STRIPPED_SENTINEL
    situs, scity = _maybe(gdf, *situs_cands), _maybe(gdf, *situs_city_cands)
    if situs and scity:
        out["situs_address"] = (_s(gdf[situs]) + ", " + _s(gdf[scity])).str.strip(", ") \
            .str.split().str.join(" ").replace("", None)
    elif situs:
        out["situs_address"] = _s(gdf[situs]).replace("", None)
    else:
        out["situs_address"] = None
    mail = _maybe(gdf, *mail_cands)
    out["mailing_address"] = _s(gdf[mail]).replace("", None) if mail else None
    out["assessed_value"] = None  # not exposed on these public views
    out["land_use"] = None
    acre = _maybe(gdf, *acre_cands)
    out["acreage"] = pd.to_numeric(gdf[acre], errors="coerce") if acre else None
    out["source_url"] = GEOM_ROWS[county]["query_url"]
    out["source_vintage"] = None
    out["retrieved_at"] = _now_iso()
    _save(out, fips, county, {
        "county": name, "state": "OR", "note": GEOM_ROWS[county]["note"],
        "source_url": GEOM_ROWS[county]["query_url"], "source_last_edit": None,
        "owner_fields": "SENTINEL (owner stripped)", "value_field": "",
        "retrieved_at": out["retrieved_at"].iloc[0], "source_vintage": None,
        "field_map": {"owner": "sentinel:[SUPPRESSED]", "apn": apn, "situs_address": situs,
                      "mailing_address": mail, "acreage": acre}})


def build_clackamas():
    _build_or_sentinel("clackamas", "41005", "Clackamas",
                       ["PARCEL_NUMBER", "TLNO", "MapTaxlot", "Taxlot"],
                       ["SITUS", "SITUS_ADDR"], ["SITUS_CITY"], ["MAILADD1"], ["TaxlotAcre", "ACRES"])


def build_tillamook():
    _build_or_sentinel("tillamook", "41057", "Tillamook",
                       ["MapTaxlot", "Taxlot"], ["SITUS", "SITUSALL"], [],
                       ["MAILADD1"], ["TaxlotAcre", "MapAcres"])


def build_morrow():
    _build_or_sentinel("morrow", "41049", "Morrow",
                       ["MapTaxlot", "Taxlot", "MAPTAXLOT", "TAXLOT"],
                       ["SITUS", "SITUS_ADDR", "situs"], ["SITUS_CITY"],
                       ["MAILADD1", "M_ADDRESS"], ["TaxlotAcre", "TAXLOTACRE", "ACRES"])


# Lincoln OR: county runs UMN MapServer (no Esri REST), but its WFS 2.0 endpoint serves the
# FULL attribute table including owner (ownname) and reprojects server-side to 4326.
LINCOLN_OR_WFS = ("https://maps.co.lincoln.or.us/fcgi-bin/mapserv.exe"
                  "?MAP=C:/ms4w/apps/gm3-lincoln-data/a_assessment.map"
                  "&SERVICE=WFS&VERSION=2.0.0&REQUEST=GetFeature&TYPENAMES=Taxlots"
                  "&SRSNAME=EPSG:4326&OUTPUTFORMAT=geojson")


def build_lincoln_or():
    import io
    import requests
    pages, offset = [], 0
    while True:
        url = LINCOLN_OR_WFS + f"&COUNT=1000&STARTINDEX={offset}"
        last = None
        for attempt in range(3):
            try:
                r = requests.get(url, headers={"User-Agent": ingest.UA["User-Agent"]}, timeout=300)
                r.raise_for_status()
                break
            except Exception as e:
                last = e
                import time
                time.sleep(3 * (attempt + 1))
        else:
            raise RuntimeError(f"WFS page failed at {offset}: {last}")
        g = gpd.read_file(io.BytesIO(r.content))
        if len(g) == 0:
            break
        pages.append(g)
        offset += len(g)
        print(f"    OR Lincoln 41041: {offset} features (WFS)", flush=True)
    gdf = pd.concat(pages, ignore_index=True)
    if gdf.crs is None:
        gdf = gdf.set_crs(4326)
    print(f"  WFS total: {len(gdf):,} taxlots")

    out = gpd.GeoDataFrame(geometry=gdf.geometry, crs=gdf.crs)
    out["state"], out["county_fips"], out["county_name"] = "OR", "41041", "Lincoln"
    out["apn"] = _s(gdf["parcelid"]).replace("", None)
    out["owner"] = _s(gdf["ownname"]).replace("", None)
    out["situs_address"] = _s(gdf["situsall"]).replace("", None)
    out["mailing_address"] = (_s(gdf["address1"]) + ", " + _s(gdf["ctystzip"])
                              ).str.strip(", ").str.split().str.join(" ").replace("", None)
    out["assessed_value"] = None  # assessment values not in the WFS layer
    out["land_use"] = None
    out["acreage"] = pd.to_numeric(gdf["mapacres"], errors="coerce")
    out["source_url"] = LINCOLN_OR_WFS.split("&SERVICE")[0]
    out["source_vintage"] = None
    out["retrieved_at"] = _now_iso()
    _save(out, "41041", "lincoln_or", {
        "county": "Lincoln", "state": "OR",
        "note": "WFS COUNTY: UMN MapServer (no Esri REST); WFS 2.0 GetFeature serves full attributes "
                "incl owner (ownname) with server-side 4326 reprojection. The first wfs-method county "
                "in the dataset — same machinery applies to the OK okmaps class.",
        "source_url": out["source_url"].iloc[0], "source_last_edit": None,
        "owner_fields": "ownname (WFS)", "value_field": "",
        "retrieved_at": out["retrieved_at"].iloc[0], "source_vintage": None,
        "field_map": {"owner": "ownname", "apn": "parcelid", "situs_address": "situsall",
                      "mailing_address": "address1+ctystzip", "acreage": "mapacres"}})


# Florida: the statewide cadastral as the state's own bulk FGDB (REST extraction was
# throttled to a crawl by AGOL). ONE sequential Arrow-streamed pass over 10.8M rows,
# flushing each county's raw parquet as its CO_NO block completes (the GDB is loaded
# county-ordered; verified). Normalization runs through the standard normalize.py.
FL_GDB = os.path.join(BULK, "fl_gdb", "CADASTRAL_DOR.gdb")
FL_COLS = ["CO_NO", "PARCEL_ID", "OWN_NAME", "OWN_ADDR1", "OWN_ADDR2", "OWN_CITY", "OWN_STATE",
           "OWN_ZIPCD", "JV", "AV_SD", "LND_VAL", "DOR_UC", "PHY_ADDR1", "PHY_ADDR2", "PHY_CITY",
           "PHY_ZIPCD", "ASMNT_YR", "LND_SQFOOT", "SALE_PRC1", "SALE_YR1", "ACT_YR_BLT", "TOT_LVG_AR"]


def build_florida():
    import csv as _csv
    import pyarrow as pa
    import pyogrio
    from ingest import FL_DOR_BY_NAME
    with open(os.path.join(HERE, "county_methods.csv"), encoding="utf-8") as f:
        fl_rows = {r["county"]: r for r in _csv.DictReader(f) if r["state"] == "FL"}
    by_co_no = {FL_DOR_BY_NAME[name]: row for name, row in fl_rows.items()}

    import pyarrow.compute as pc
    import pyarrow.parquet as pq
    tmp_dir = os.path.join(BULK, "fl_split")
    os.makedirs(tmp_dir, exist_ok=True)

    # PASS 1 — single sequential stream; one incremental parquet writer per county.
    # No ordering assumption (the GDB is mostly county-ordered but strays exist), and
    # memory stays constant: a batch is split by CO_NO and appended to each writer.
    writers, n, geom_name = {}, 0, "Shape"
    with pyogrio.open_arrow(FL_GDB, columns=FL_COLS, use_pyarrow=True) as (meta, reader):
        geom_name = meta.get("geometry_name") or "Shape"
        for batch in reader:
            tbl = pa.Table.from_batches([batch])
            n += tbl.num_rows
            for co in pc.unique(tbl.column("CO_NO")).to_pylist():
                part = tbl.filter(pc.equal(tbl.column("CO_NO"), co))
                co = int(co) if co is not None else -1
                if co not in writers:
                    writers[co] = pq.ParquetWriter(
                        os.path.join(tmp_dir, f"co_{co}.parquet"), part.schema,
                        compression="zstd")
                writers[co].write_table(part)
            if n % 1_000_000 < tbl.num_rows:
                print(f"    FL stream: {n:,}/10,831,924 rows", flush=True)
    for w in writers.values():
        w.close()
    print(f"  FL pass 1 complete: {n:,} rows into {len(writers)} county files", flush=True)

    # PASS 2 — per county: WKB -> geometry, reproject, write the standard raw parquet.
    for co in sorted(writers):
        tmp = os.path.join(tmp_dir, f"co_{co}.parquet")
        row = by_co_no.get(co)
        if row is None:
            print(f"  CO_NO={co}: not a county ({pq.ParquetFile(tmp).metadata.num_rows} rows) — skipped", flush=True)
            continue
        df = pd.read_parquet(tmp)
        geom = gpd.GeoSeries.from_wkb(df.pop(geom_name), crs="EPSG:6439").to_crs(4326)
        gdf = gpd.GeoDataFrame(df, geometry=geom)
        slug = row["county"].lower().replace(" ", "_").replace(".", "").replace("-", "_")
        out = os.path.join(RAW, f"{row['fips']}_{slug}.parquet")
        gdf.to_parquet(out, compression="zstd")
        ingest.update_manifest(row["fips"], {
            "fips": row["fips"], "county": row["county"], "state": "FL",
            "file": os.path.relpath(out, ROOT).replace("\\", "/"),
            "feature_count": int(len(gdf)), "reported_count": None,
            "retrieved_at": _now_iso(), "source_last_edit": "2026-05-05 (state FGDB)",
            "source_url": "https://publicfiles.dep.state.fl.us/otis/gis/data/Cadastral_Statewide.zip",
            "owner_fields": "OWN_NAME", "value_field": "JV",
            "note": "STATE BULK FGDB (DOR NAL schema; JV = just/market value). REST layer throttled; "
                    "the state's own zip is the refreshable source (refresh.py HEADs Last-Modified).",
        }, MANIFEST)
        os.unlink(tmp)
        print(f"  DONE {row['fips']} {row['county']}: {len(gdf):,} parcels", flush=True)


def build_statewide(county):
    """Adams/Garfield/Lincoln: geometry+situs+values from the statewide layer, owner sentinel."""
    row = GEOM_ROWS[county]
    fips, name = row["fips"], row["county"]
    gdf = gpd.read_parquet(os.path.join(RAW, f"{fips}_{county}.parquet"))
    print(f"  statewide slice: {len(gdf):,} parcels")
    out = gpd.GeoDataFrame(geometry=gdf.geometry, crs=gdf.crs)
    out["state"], out["county_fips"], out["county_name"] = "WA", fips, name
    out["apn"] = _s(gdf["PARCEL_ID_NR"]).replace("", None)
    out["owner"] = NOT_PUBLISHED_SENTINEL
    out["situs_address"] = (gdf["SITUS_ADDRESS"].fillna("").str.strip() + ", "
                            + gdf["SITUS_CITY_NM"].fillna("").str.strip()
                            ).str.strip(", ").str.split().str.join(" ").replace("", None)
    out["mailing_address"] = None
    out["assessed_value"] = (pd.to_numeric(gdf["VALUE_LAND"], errors="coerce").fillna(0)
                             + pd.to_numeric(gdf["VALUE_BLDG"], errors="coerce").fillna(0)).replace(0, None)
    out["land_use"] = gdf["LANDUSE_CD"]
    out["acreage"] = None
    out["source_url"] = STATEWIDE_URL
    out["source_vintage"] = None  # statewide layer lastEditDate is the refresh signal
    out["retrieved_at"] = _now_iso()
    _save(out, fips, county, {
        "county": name, "state": "WA", "note": NOTES["STATEWIDE_BACKFILL"],
        "source_url": STATEWIDE_URL, "owner_fields": "SENTINEL (not published anywhere free)",
        "value_field": "VALUE_LAND+VALUE_BLDG", "retrieved_at": out["retrieved_at"].iloc[0],
        "field_map": {"owner": "sentinel:[NOT PUBLISHED]", "assessed_value": "VALUE_LAND+VALUE_BLDG",
                      "apn": "PARCEL_ID_NR", "situs_address": "SITUS_ADDRESS+SITUS_CITY_NM",
                      "land_use": "LANDUSE_CD"}})


BUILDERS = {"king": build_king, "pierce": build_pierce, "clark": build_clark,
            "grays_harbor": build_grays_harbor, "pacific": build_pacific,
            "adams": lambda: build_statewide("adams"),
            "garfield": lambda: build_statewide("garfield"),
            "lincoln": lambda: build_statewide("lincoln"),
            "lincoln_or": build_lincoln_or,
            "clackamas": build_clackamas, "tillamook": build_tillamook, "morrow": build_morrow,
            "florida": build_florida}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--county", required=True, choices=sorted(BUILDERS))
    ap.add_argument("--geometry-only", action="store_true")
    ap.add_argument("--refresh", action="store_true",
                    help="re-pull geometry AND re-download the bulk source zips")
    args = ap.parse_args()
    download_sources(args.county, force=args.refresh)
    pull_geometry(args.county, refresh=args.refresh)
    if not args.geometry_only:
        BUILDERS[args.county]()


if __name__ == "__main__":
    main()
