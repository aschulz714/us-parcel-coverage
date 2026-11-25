"""LAND-USE STANDARDIZATION — one vocabulary across every county.

Adds two columns to every normalized county parquet:
  land_use_code      two-digit WA DOR code (WAC 458-53-030; the table lives in
                     data/landuse_codes/wa_dor.csv with official labels + categories)
  land_use_category  ~9 coarse buckets (residential / industrial / commercial-trade /
                     agriculture-forest / undeveloped ...), identical statewide

Per-county method, decided by MEASURING the county's own data (recorded in the manifest):
  dor-parse       >=80% of the county's non-null raw land_use values begin with a VALID
                  DOR code ('11', '83.0', '11 - Residential...') -> parse it directly.
                  The threshold guards against local code systems that collide by accident
                  (King's PresentUse, Cowlitz's '101') — those fail validation county-wide.
  statewide-join  everything else: join LANDUSE_CD from WaTech's statewide Parcels_2026
                  layer by APN (= statewide ORIG_PARCEL_ID; join proven in the Pacific
                  currency audit). Slices cached in data/parcels/landuse/<fips>.parquet.
Raw county land_use is PRESERVED; coverage of both new columns is measured and stored in
the manifest (landuse_method / landuse_coverage) — standardize, never overwrite.

NATIONAL DESIGN (how this scales): land_use_category is the cross-state constant; the
code system + lookup CSV is PER-STATE (data/landuse_codes/<st>_<std>.csv — FL DOR use
codes, etc.). Every county additionally runs the NATIONAL KEYWORD classifier (classify_text)
over land_use AND land_use_desc as a fallback — so states with no code table still get a
category from readable text ("SINGLE FAMILY"->residential, "VACANT LAND"->undeveloped/open),
and code-table states fill their text-valued and untabled rows too. Waterfall per parcel:
state code table -> keyword(land_use) -> keyword(land_use_desc). Numeric codes never keyword-
match, so a code is never guessed; unrecognized values stay null (honest). Effort scales per
STATE for the precise code layer; the keyword layer is free and national.

Usage:
  python src/landuse_std.py                # all counties in the manifest
  python src/landuse_std.py --state NJ     # one state
  python src/landuse_std.py --fips 53033
"""
import argparse, json, os, re, sys, time

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import requests

from atomic_io import atomic_replace_writer

HERE = os.path.dirname(__file__)
ROOT = os.path.dirname(HERE)
PARCELS = os.path.join(ROOT, "data", "parcels")
MANIFEST = os.path.join(PARCELS, "manifest.json")
LU_CACHE = os.path.join(PARCELS, "landuse")
CODES_CSV = os.path.join(ROOT, "data", "landuse_codes", "wa_dor.csv")
STATEWIDE = ("https://services.arcgis.com/jsIt88o09Q0r1j8h/arcgis/rest/services/"
             "Current_Parcels/FeatureServer/0/query")
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"}
PARSE_THRESHOLD = 0.80


def _codes():
    df = pd.read_csv(CODES_CSV, dtype=str)
    return dict(zip(df["code"], df["category"])), dict(zip(df["code"], df["label"]))


def _extract_code(series, valid):
    """Leading two digits of the raw value, kept only when they form a VALID DOR code."""
    code = series.fillna("").astype(str).str.extract(r"^\s*(\d{2})", expand=False)
    return code.where(code.isin(valid))


def _statewide_slice(fips):
    """APN -> LANDUSE_CD for one county from the statewide layer (cached; no geometry)."""
    os.makedirs(LU_CACHE, exist_ok=True)
    cache = os.path.join(LU_CACHE, f"{fips}.parquet")
    if os.path.exists(cache):
        return pd.read_parquet(cache)
    rows, offset = [], 0
    while True:
        last = None
        for attempt in range(4):  # the AGOL host 502s transiently under sustained paging
            try:
                r = requests.get(STATEWIDE, params={
                    "where": f"FIPS_NR='{fips[2:]}'", "outFields": "ORIG_PARCEL_ID,LANDUSE_CD",
                    "returnGeometry": "false", "f": "json", "resultOffset": offset,
                    "resultRecordCount": 2000, "orderByFields": "OBJECTID"}, headers=UA, timeout=120)
                r.raise_for_status()
                break
            except Exception as e:
                last = e
                time.sleep(3 * (attempt + 1))
        else:
            raise RuntimeError(f"statewide slice failed at offset {offset}: {last}")
        feats = r.json().get("features", [])
        if not feats:
            break
        rows.extend(f["attributes"] for f in feats)
        offset += len(feats)
        time.sleep(0.2)
    if not rows:  # Asotin is currently ABSENT from the statewide layer (38/39 FIPS present)
        df = pd.DataFrame({"apn": pd.Series(dtype=str), "lu": pd.Series(dtype=str)})
        df.to_parquet(cache)
        return df
    df = pd.DataFrame(rows).dropna(subset=["ORIG_PARCEL_ID"])
    df["ORIG_PARCEL_ID"] = df["ORIG_PARCEL_ID"].astype(str).str.strip()
    df = df.drop_duplicates(subset=["ORIG_PARCEL_ID"]).rename(
        columns={"ORIG_PARCEL_ID": "apn", "LANDUSE_CD": "lu"})
    df.to_parquet(cache)
    return df


# Per-state code tables beyond WA (built+source-cited by the config helper session,
# 2026-06-12) with the value->code parser rule each state's data needs:
#   3digit — values are (possibly float-cast) numerics; normalize to zero-padded 3 digits
#   prefix — CADs append local suffixes (TX: A1/A11/'A1,A1'); longest table-code prefix wins
STATE_TABLES = {
    "FL": ("fl_dor.csv", "3digit"),
    "TX": ("tx_ptad.csv", "prefix"),
    "OR": ("or_propclass.csv", "3digit"),
    "TN": ("tn_sluc.csv", "3digit"),  # SLUCM-derived statewide table; numeric codes are
                                      # mostly Hamilton today (other TN counties publish
                                      # free text like 'RESIDENTIAL', which nulls honestly)
    # 2026-06-12 activation wave (tables measured + Phase-2-verified by the land-use session):
    "AR": ("ar_parceltype.csv", "text"),     # ACD parceltype 2-char codes (99.9%)
    "ID": ("id_whitestar.csv", "digit1"),    # WhiteStar 4-digit -> first-digit semantics (93.7%)
    "MT": ("mt_proptype.csv", "text"),       # appraisal-type labels; honest partial (46.6%)
    "NC": ("nc_text.csv", "text"),           # 300-key exact-text table over parusedesc (66%)
    "WI": ("wi_propclass.csv", "multiclass"),# Wis. Stat. 70.32(2)(a); comma-multi-class
    "WV": ("wv_ias.csv", "wv-join"),         # statewide IAS LandUseCode via ParcelSummary join
    "OH": ("oh_dte.csv", "3digit"),          # DTE 3-digit standard; only the 7 self-host metros
                                             # carry a use code (81 ODNR slices have none -> 0%)
    "IN": ("in_dlgf.csv", "3digit"),         # DLGF Code List 1 (property class code, 50 IAC 26-20-3);
                                             # 3-digit, on the IndianaMap statewide layer's
                                             # dlgf_prop_class_code (+ some county-direct layers).
                                             # County-local junk (999 etc.) unmatched -> honest null.
    "NY": ("ny_propclass.csv", "3digit"),    # NYS Property Type Classification (3-digit). The 5 NYC
                                             # boroughs use 2-digit PLUTO codes (zero-pad to 001-011,
                                             # absent from the table) -> left null on purpose.
    "MA": ("ma_classcodes.csv", "prefix"),   # MA DOR class codes; 3-digit base + 4-digit local
                                             # extension -> prefix rule matches via the 3-digit base.
    "UT": ("ut_landtype.csv", "text"),       # UT statewide free-text land types (37 values); exact
                                             # text match. Unknown/Exempt/Personal-Property/valuation
                                             # flags (NON-FAA/FAA Mkt) carry no use -> left null.
    "NJ": ("nj_propclass.csv", "text"),      # NJ MOD-IV property classification (N.J.A.C. 18:12-2.2),
                                             # statewide standard ('2' residential, '4A' commercial,
                                             # '15C' public). 15F Other-Exempt carries no use -> null.
    "ME": ("ma_classcodes.csv", "prefix"),   # ME towns (Vision-served) use MA DOR-style codes with a
                                             # local 4th digit (1010 Single Fam, 1020 Condo, 1030 Mobile
                                             # Home — verified against Cumberland's LAND_USE_D text,
                                             # 2026-07-03). Short town codes ('16','IR1') miss -> null.
}
WV_SUMMARY = ("https://services.wvgis.wvu.edu/arcgis/rest/services/Planning_Cadastre/"
              "WV_Parcels/MapServer/11/query")
# WI multiclass precedence (session spec): pick the highest-priority class present
WI_PRECEDENCE = ["3", "2", "1", "4", "5M", "6", "5", "7"]
WI_SKIP = {"AW", "AWO"}


def _table_lookup_codes(state):
    import pandas as _pd
    fname, rule = STATE_TABLES[state]
    df = _pd.read_csv(os.path.join(ROOT, "data", "landuse_codes", fname), dtype=str)
    cat = {r["code"]: (r["category"] if isinstance(r["category"], str) and r["category"].strip()
                       else None) for _, r in df.iterrows()}
    if rule == "text":  # exact-text tables match case-insensitively (MT mixed-case labels)
        cat = {str(k).upper(): v for k, v in cat.items()}
    return cat, rule


def _parse_code_series(raw, codes, rule):
    valid = set(codes)
    if rule == "text":
        # keys already uppercased by _table_lookup_codes; match case-insensitively
        valid = {str(c).upper() for c in valid}
        # exact normalized match only — NO substrings ("RES" would hit "PRESBYTERIAN")
        def p(v):
            if not v:
                return None
            t = re.sub(r"\s+", " ", str(v)).strip().upper()
            return t if t in valid else None
        return raw.map(p)
    if rule == "digit1":
        def p(v):
            if not v:
                return None
            m = re.match(r"^\s*(\d{4})(?:\.0*)?\s*$", str(v))
            return m.group(1)[0] if m and m.group(1)[0] in valid else None
        return raw.map(p)
    if rule == "multiclass":
        # WI: comma-separated class lists ('1,6'); precedence picks the dominant class
        def p(v):
            if not v:
                return None
            toks = [re.sub(r"\s+", "", t).upper() for t in str(v).split(",")]
            toks = [t for t in toks if t and t not in WI_SKIP]
            for want in WI_PRECEDENCE:
                if want in toks:
                    return want
            return next((t for t in toks if t in valid), None)
        return raw.map(p)
    if rule == "3digit":
        def p(v):
            if not v:
                return None
            m = re.match(r"^\s*(\d+)(?:\.0*)?\s*$", str(v))
            if not m:
                return None
            c = m.group(1).zfill(3)
            return c if c in valid else None
        return raw.map(p)
    # prefix rule: take the first comma-piece, strip non-alnum, longest valid prefix
    by_len = sorted(valid, key=len, reverse=True)
    def p(v):
        if not v:
            return None
        tok = re.sub(r"[^A-Z0-9]", "", str(v).upper().split(",")[0])
        if not tok:
            return None
        return next((c for c in by_len if tok.startswith(c)), None)
    return raw.map(p)


# ============================================================================================
# National KEYWORD classifier — the cross-state text fallback the docstring always promised but
# that was never built. Maps readable free text (in land_use OR land_use_desc, any state) to the
# same 8-bucket vocabulary the code tables use. Ordered rules, FIRST match wins; the order encodes
# precedence for compound labels ("MOBILE HOME PARK"=residential not park; "VACANT RESIDENTIAL"=
# residential by intended use, matching FL DOR 000; bare "VACANT LAND"=undeveloped/open). Precision
# audited against the national value head 2026-06-28 (recon/landuse/_classify_dev.py) — exact-ish
# keywords only, numerics never match (no code is ever guessed), "NON-RES" neutralized.
# ============================================================================================
_KW_RULES = [
    (r"MOBILE HOME PARK|TRAILER PARK|MANUFACTURED HOME (PARK|COMMUNIT)|\bRV PARK|MH PARK", "residential"),
    (r"AGRICULTUR|\bFARM|RANCH|TIMBER|SILVICULT|ORCHARD|VINEYARD|\bGROVE|PASTURE|\bCROP|"
     r"LIVESTOCK|POULTRY|\bDAIR|GRAZING|\bAG\b|\bAG USE|\bAG-|HORTICULT|\bNURSER|GREENHOUSE|"
     r"TREE FARM|FORESTRY|MANAGED FOREST|\bFOREST\b|IRRIGAT|FEEDLOT|\bSOD\b", "agriculture/forest"),
    (r"RESIDEN|SINGLE.?FAM|MULTI.?FAM|\bSFR\b|\bSFD\b|\bMFR\b|\bDUPLEX|TRIPLEX|FOURPLEX|"
     r"QUADPLEX|\bAPARTMENT|\bAPTS?\b|\bCONDO|TOWNHO|\bDWELL|MOBILE HOME|MANUFACTURED HO(ME|USING)|"
     r"\bMFG HOME|\bTRAILER\b|SEASONAL RES|\bCOTTAGE|BUNGALOW|\bFAMILY\b|\bRES\b|\bSF DETACH|"
     r"\bSF RES|\bMF RES|GARDEN APT|ROWHOUSE|\bVILLA\b", "residential"),
    (r"INDUSTR|MANUFACTUR|WAREHOUS|\bFACTORY|REFINER|FOUNDRY|FABRICAT|\bMILL\b|SAWMILL|"
     r"DISTRIBUTION|PROCESSING|\bQUARR|GRAVEL PIT|\bMINE\b|MINING|LUMBER ?YARD|\bLT IND|"
     r"\bHVY IND|LIGHT INDUST|HEAVY INDUST|\bPLANT\b", "industrial"),
    (r"UTILIT|\bUTIL\b|RIGHT.?OF.?WAY|\bR ?O ?W\b|RAILROAD|RAILWAY|\bRAIL\b|PIPELINE|"
     r"TRANSMISSION|SUBSTATION|\bELECTRIC|POWER ?LINE|\bSEWER|WATER (TREATMENT|DISTRICT|WORKS)|"
     r"TELEPHONE|TELECOM|CELL TOWER|COMMUNICATION TOWER|\bAIRPORT|\bHELIPORT|TRANSIT|"
     r"PARKING (LOT|GARAGE|STRUCT)|\bDOCK\b|\bHARBOR|\bMARINA", "transport/utilities"),
    (r"CHURCH|RELIGIOUS|WORSHIP|\bTEMPLE|SYNAGOG|MOSQUE|PARSONAGE|\bSCHOOL|\bCOLLEGE|UNIVERSIT|"
     r"\bEDUCAT|ACADEMY|\bPARK\b|\bPARKS\b|RECREATION|\bGOLF|\bCEMETER|MAUSOLEUM|\bMUSEUM|"
     r"LIBRARY|\bHOSPITAL|GOVERNMENT|\bGOVT\b|MUNICIPAL|\bPUBLIC\b|\bEXEMPT|INSTITUTION|"
     r"CHARIT|NON.?PROFIT|FRATERNAL|\bLODGE\b|\bCLUB\b|COMMUNITY CENTER|FIRE (STATION|DEPT|DIST)|"
     r"POLICE|\bPRISON|\bJAIL|COURT ?HOUSE|CONSERVAT|WILDLIFE|PRESERVE|SANCTUARY|\bMILITARY|"
     r"ARMORY|POST OFFICE|\bCIVIC|AUDITORIUM|\bTHEATER", "recreation/cultural"),
    (r"\bOFFICE|\bBANK\b|MEDICAL|\bCLINIC|\bHOTEL|\bMOTEL|RESTAURANT|\bCAFE|\bSALON|FUNERAL|"
     r"NURSING HOME|ASSISTED LIVING|CONVALESCENT|DAY ?CARE|VETERINAR|PROFESSIONAL|"
     r"SERVICE STATION|AUTO (REPAIR|SERVICE|SALES)|CAR WASH|\bLAUNDR|\bBANKING|\bGARAGE\b", "commercial-services"),
    (r"COMMERCIAL|\bRETAIL|\bSTORE\b|\bSHOP\b|SHOPPING|\bMALL\b|SUPERMARKET|\bMARKET\b|"
     r"CONVENIENCE|GAS STATION|DEPARTMENT STORE|MERCANTILE|DEALERSHIP|\bSALES\b|\bTAVERN|"
     r"\bBAR\b|\bPUB\b|\bMERCH|WHOLESALE", "commercial-trade"),
    (r"VACANT|\bVAC\b|UNIMPROVED|\bUNIMPR|RAW LAND|UNDEVELOPED|OPEN SPACE|\bACREAGE|"
     r"WASTE ?LAND|GREENBELT|\bBARREN|SUBMERGED|\bDESERT|\bMARSH|WETLAND|FLOOD ?PLAIN|"
     r"\bVL\b|\bVACNT", "undeveloped/open"),
]
_KW_RULES = [(re.compile(rx), cat) for rx, cat in _KW_RULES]
_KW_SKIP = {"", "0", "00", "000", "UNKNOWN", "NONE", "NA", "N A", "NULL", "NO"}


def classify_text(text):
    """One free-text value -> national category, or None when nothing matches (never guesses)."""
    if text is None:
        return None
    t = re.sub(r"[^A-Z0-9 ]", " ", str(text).upper())
    t = re.sub(r"\s+", " ", t).strip()
    if t in _KW_SKIP:
        return None
    # neutralize "NON-RESIDENTIAL"/"NON RES" so the RES/RESIDEN rules can't false-fire on it
    t = re.sub(r"\bNON RESIDENTIAL\b|\bNON RES\b|\bNONRESID\w*", " NONRES ", t)
    for rx, cat in _KW_RULES:
        if rx.search(t):
            return cat
    return None


def _keyword_fill(category, land_use, land_use_desc):
    """Fill the still-null rows of `category` (Series) from keyword(land_use) then keyword(desc).
    A distinct-value cache keeps this fast over millions of rows (the value head is tiny)."""
    cache = {}
    def kw(v):
        if v not in cache:
            cache[v] = classify_text(v)
        return cache[v]
    need = category.isna()
    if not need.any():
        return category
    filled = land_use[need].map(kw)
    if land_use_desc is not None:
        filled = filled.fillna(land_use_desc[need].map(kw))
    category = category.copy()
    category.loc[need] = filled
    return category


def _meta_with_cols(meta, cols):
    """Register added string cols in the parquet pandas metadata, preserving geoparquet 'geo' meta."""
    if not meta or b"pandas" not in meta:
        return meta
    pm = json.loads(meta[b"pandas"])
    have = {c["name"] for c in pm.get("columns", [])}
    for col in cols:
        if col not in have:
            pm["columns"].append({"name": col, "field_name": col, "pandas_type": "unicode",
                                  "numpy_type": "object", "metadata": None})
    out = dict(meta)
    out[b"pandas"] = json.dumps(pm).encode()
    return out


def _write_columns(path, table, newcols):
    """Append/replace string columns on a parquet, geometry untouched (opaque bytes). newcols =
    {name: pandas Series}. ~10x faster than a geopandas round-trip (see mailing_std)."""
    for name, ser in newcols.items():
        arr = pa.array(ser.where(ser.notna(), None).astype(object), type=pa.string())
        if name in table.column_names:
            table = table.drop([name])
        table = table.append_column(name, arr)
    table = table.replace_schema_metadata(_meta_with_cols(table.schema.metadata, list(newcols)))
    atomic_replace_writer(
        path, lambda temp: pq.write_table(table, temp, compression="zstd"))


def _wv_codes_by_apn(entry, valid):
    """WV: geometry layer has no use field; statewide ParcelSummary table 11 keys LandUseCode by
    CleanParcelID (= our apn since the 2026-06-12 re-normalize). CountyCode is an INTEGER there."""
    os.makedirs(LU_CACHE, exist_ok=True)
    cache = os.path.join(LU_CACHE, f"{entry['fips']}.parquet")
    if os.path.exists(cache):
        sl = pd.read_parquet(cache)
    else:
        rows, offset = [], 0
        while True:
            for attempt in range(4):
                try:
                    r = requests.get(WV_SUMMARY, params={
                        # CountyCode is ALPHABETICAL-sequential 1-55, not the FIPS number; WV
                        # county FIPS are odd 001..109 -> (fips+1)/2
                        "where": f"CountyCode={(int(entry['fips'][2:]) + 1) // 2}",
                        "outFields": "CleanParcelID,LandUseCode", "returnGeometry": "false",
                        "f": "json", "resultOffset": offset, "resultRecordCount": 2000,
                        "orderByFields": "ID"}, headers=UA, timeout=120)  # table id field is ID
                    r.raise_for_status()
                    d = r.json()
                    if d.get("error"):
                        raise RuntimeError(d["error"].get("message"))
                    break
                except Exception:
                    time.sleep(3 * (attempt + 1))
            feats = d.get("features", [])
            if not feats:
                break
            rows.extend(f["attributes"] for f in feats)
            offset += len(feats)
            time.sleep(0.2)
        if not rows:
            raise RuntimeError(f"WV ParcelSummary slice empty for {entry['fips']} "
                               "(refusing to cache an empty join)")
        sl = (pd.DataFrame(rows, columns=["CleanParcelID", "LandUseCode"])
              .dropna(subset=["CleanParcelID"]).drop_duplicates("CleanParcelID"))
        sl.to_parquet(cache)
    return dict(zip(sl["CleanParcelID"].astype(str).str.strip(),
                    sl["LandUseCode"].fillna("").astype(str).str.strip().str.zfill(3)))


def apply_county(entry, cat_by_code=None, label_by_code=None):
    """Standardize one county in place. Writes land_use_code (where a code system matched) +
    land_use_category (code first, then the national keyword fallback over land_use & land_use_desc).
    pyarrow I/O: geometry stays opaque bytes (~10x faster than geopandas). Returns (method, coverage)."""
    st = entry["state"].upper()
    path = os.path.join(ROOT, entry["normalized_file"])
    table = pq.read_table(path)
    n = table.num_rows
    cols = table.column_names
    land_use = (table.column("land_use").to_pandas() if "land_use" in cols
                else pd.Series([None] * n, dtype=object))
    land_use_desc = table.column("land_use_desc").to_pandas() if "land_use_desc" in cols else None
    apn = (table.column("apn").to_pandas() if "apn" in cols else pd.Series([None] * n, dtype=object))

    code = pd.Series([None] * n, dtype=object, index=land_use.index)
    cat_map, method, note = {}, "keyword", ""

    if st in STATE_TABLES:
        cat_map, rule = _table_lookup_codes(st)
        if rule == "wv-join":
            lut = _wv_codes_by_apn(entry, set(cat_map))
            code = apn.astype(str).str.strip().map(lut)
            code = code.where(code.isin(set(cat_map)))
        else:
            code = _parse_code_series(land_use, cat_map, rule)
        method = f"table-lookup-{rule}"
    elif st == "WA":
        if cat_by_code is None:
            cat_by_code, label_by_code = _codes()
        cat_map = cat_by_code
        valid = set(cat_by_code)
        parsed = _extract_code(land_use, valid)
        nonnull = int(land_use.notna().sum())
        parse_rate = float(parsed.notna().sum()) / nonnull if nonnull else 0.0
        if nonnull and parse_rate >= PARSE_THRESHOLD:
            method, code = "dor-parse", parsed
        else:
            method = "statewide-join"
            sw = _statewide_slice(entry["fips"])
            lu = _extract_code(sw.set_index("apn")["lu"], valid)
            code = apn.astype(str).str.strip().map(lu).fillna(parsed)  # parseable raw where join missed
        note = f"  (raw parse rate {parse_rate:.0%})"

    # code -> category, then the NATIONAL keyword fallback (land_use, then land_use_desc) — for
    # EVERY state, including the ~22 with no code table (which start from an all-null category).
    category = code.map(cat_map) if cat_map else pd.Series([None] * n, dtype=object, index=code.index)
    code_cov = float(category.notna().mean())
    category = _keyword_fill(category, land_use, land_use_desc)
    coverage = round(float(category.notna().mean()), 3)

    _write_columns(path, table, {"land_use_code": code, "land_use_category": category})
    print(f"  {st} {entry['county']:<16} {entry['fips']}  {method:<18} "
          f"category {coverage:.0%}  (code {code_cov:.0%} + keyword {coverage - code_cov:+.0%}){note}",
          flush=True)
    return method, coverage


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--state")
    ap.add_argument("--fips", nargs="*")
    args = ap.parse_args()
    with open(MANIFEST, encoding="utf-8") as f:
        manifest = json.load(f)
    entries = [dict(e, fips=fips) for fips, e in manifest.items() if e.get("normalized_file")]
    if args.state:
        entries = [e for e in entries if e["state"].upper() == args.state.upper()]
    if args.fips:
        entries = [e for e in entries if e["fips"] in set(args.fips)]
    cat_by_code, label_by_code = _codes()
    print(f"Standardizing land use for {len(entries)} counties "
          f"({len(cat_by_code)} DOR codes, threshold {PARSE_THRESHOLD:.0%})...")
    failed = []
    for e in sorted(entries, key=lambda x: x["fips"]):
        try:
            method, coverage = apply_county(e, cat_by_code, label_by_code)
            import ingest as _ingest  # lockfile-guarded manifest write (concurrent-run safe)
            _ingest.update_manifest(e["fips"], {"landuse_method": method,
                                                "landuse_coverage": coverage}, MANIFEST)
        except Exception as ex:
            failed.append(e["fips"])
            print(f"  FAIL {e['fips']} {e['county']}: {ex}", flush=True)
    if failed:
        sys.exit(f"{len(failed)} counties failed: {' '.join(failed[:20])}")


if __name__ == "__main__":
    main()
