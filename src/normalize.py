"""Normalize raw county parcel pulls to ONE standard schema — the Regrid-style layer.

Reads data/parcels/raw/<fips>_*.parquet (from ingest.py) and writes
data/parcels/normalized/<fips>_*.parquet where EVERY county has identical columns:

    state, county_fips, county_name, apn, owner, situs_address, mailing_address,
    assessed_value, land_use, acreage, source_url, source_vintage, retrieved_at, geometry

Field mapping per county:
  - owner: the crosswalk's owner_fields (pipe-separated multi-field, same semantics as
    resolve.py's _owner_from — collapse padding, dedupe, join with " / ").
  - assessed_value: the crosswalk's value_field when present, else the best-named value
    column (resolve.py's _best_value name rules: reject date/year/id/zip/fips/acreage names).
  - apn / situs / mailing / land_use / acreage: ranked name-pattern heuristics over the
    county's actual columns.
  The CHOSEN mapping for every county is recorded back into data/parcels/manifest.json
  under "field_map" — nothing is silently guessed; the mapping is auditable.

Usage:
  python src/normalize.py                # everything in the manifest
  python src/normalize.py --state WA
  python src/normalize.py --fips 53069
"""
import argparse, json, os, re, sys

import geopandas as gpd
import pandas as pd

from atomic_io import atomic_replace_writer
from source_account_roles import ROLE_COLUMNS, mapping_entry, apply_account_fields

HERE = os.path.dirname(__file__)
ROOT = os.path.dirname(HERE)
PARCELS = os.path.join(ROOT, "data", "parcels")
MANIFEST = os.path.join(PARCELS, "manifest.json")

# Ranked (first match wins) name patterns per normalized column. Matched case-insensitively
# against the county's real column names; earlier patterns are more trustworthy names.
APN_PATTERNS = [r"^clean_?parcel_?id$",  # WV statewide: the unique 20-char composite — the bare
                                         # 'Parcel' field is PROVABLY non-unique (Barbour '0002' x2)
                r"^(apn|ain|pin)(_?\d)?$", r"^parcel_?id$", r"parcel_?num", r"parcelnumb", r"^parcel_?no",
                r"^(alt)?parno$",  # NC OneMap standard
                r"^swis_?print_?key_?id$",  # NY statewide tax parcels: SWIS+print-key composite is the
                r"^print_?key$", r"^sbl$",  # statewide-UNIQUE id; PRINT_KEY/SBL repeat across munis
                r"^span$",  # VT: School Property Account Number — the statewide-unique parcel id
                r"^ssl$",  # DC: Square-Suffix-Lot — the parcel id (Common Ownership Layer)
                r"_pin(_?nu?m)?$",  # prefixed PINs: Hoke NC TWN_PIN, GIS_PIN, PIN_NUM variants
                r"^gis_?link$",  # Hamilton TN assessor extract: the map/group/parcel GIS key
                r"^geographic(_id)?$",  # WA "Geographic ID" = the real parcel number (beats Property_I[D])
                r"^geo_?id$", r"^geo_?idt?$",
                r"^geo_?\d+$",  # Clallam GEO_12 = the real 12-digit parcel number (PROP_ID is internal)
                r"^(or_?|map_?)?taxlot$",  # ORMAP: MapTaxlot/ORTaxlot/Taxlot beat account numbers
                r"map_?taxlot",  # prefixed exports: Polk TaxlotTemp_MapTaxlot, Curry CurryTaxlots_MapTaxlot
                r"^map_?tax$",  # Columbia OR MAP_TAX ('7N4W1000 1001')
                r"^tl_?(no|id)$",  # Washington OR TLNO/TLID
                r"^prop_?id$", r"^property_?i", r"tax_?parcel", r"^pid$", r"^tax_?id$",
                r"^parcel$", r"^acct", r"account_?n",
                r"taxlot",  # TaxLotHist-style history columns are sparse
                # Appended low-priority (after every existing match) so they can only RECOVER
                # currently-unmapped (apn=None) counties, never re-map a county that already
                # matches above — provably zero-regression. State-standard named ids:
                r"^pin_?18",       # IN 18-digit state parcel number (pin_18 / pin_18stripped)
                r"^parcel_?pin$",  # IA Bremer / MO Wright PARCELPIN
                r"^tax_?pin$",     # MN Benton / SC Jasper TaxPIN
                r"^gpin",          # SC Oconee Geographic Parcel ID Number
                r"^pidn",          # AK Valdez-Cordova PIDN
                r"^tax_?id_?num$"] # PA Fayette taxidnum / PA Greene Tax_ID_Num
APN_EXCLUDE = r"objectid"  # AcctTable_OBJECTID-style ids are never the APN
# Per-county PARCEL-GRAIN keys for sources whose published layer is a parcel x sub-record JOIN
# (one row per improvement / assessment record, the parcel polygon repeated per record). The
# layer's declared OID is the base parcel-polygon key (non-unique); the row OID (OBJECTID_1 /
# OBJECTID_12) is unique per join row. Ingest fetches and verifies the FULL row universe (raw
# keeps every row for provenance); normalize collapses to one row per (key..., geometry),
# keep-first, and reports the drop as grain_dropped_count so refresh.py's retention gate and
# qa/validate.py can distinguish a documented grain collapse from a truncated pull.
# Discovered 2026-09-14/15: Lowndes GA served 66,316 rows = 52,033 polygons (one APN was a
# 125-polygon x 107-record cross product = 13,375 rows). Declare ONLY after inspecting the raw.
GRAIN_KEYS = {
    "13185": ["OBJECTID"],   # GA Lowndes: OBJECTID_12 row id 66,316 / OBJECTID polygon 51,424 (+geom 52,033)
    "47125": ["OBJECTID"],   # TN Montgomery: OBJECTID_1 row id 99,656 / OBJECTID+geom 92,462; dup-APN groups 100% stacked
}
GRAIN_DROPS = {}  # fips -> rows dropped by grain_collapse in THIS process (read by refresh.py)


def grain_collapse(gdf, fips):
    """Collapse a join-grain raw to parcel grain: one row per (GRAIN_KEYS[fips]..., geometry WKB),
    keep-first. Returns (gdf, dropped). No-op (dropped=0) for counties without a declared key or
    whose key columns are absent (logged — a silently skipped collapse would look like a
    duplicate-APN explosion downstream)."""
    keys = GRAIN_KEYS.get(fips)
    if not keys:
        return gdf, 0
    present = [k for k in keys if k in gdf.columns]
    if len(present) != len(keys):
        print(f"    WARNING {fips}: grain key(s) {sorted(set(keys) - set(present))} missing from raw — "
              f"grain collapse SKIPPED", flush=True)
        return gdf, 0
    before = len(gdf)
    wkb = gdf.geometry.to_wkb()
    keep = ~gdf[present].assign(_wkb=wkb.values).duplicated(keep="first")
    out = gdf[keep.values].reset_index(drop=True)
    dropped = before - len(out)
    print(f"    {fips}: grain collapse on {present}+geometry: {before:,} join rows -> {len(out):,} "
          f"parcels ({dropped:,} dropped)", flush=True)
    return out, dropped
# Per-county APN field overrides: counties whose real parcel id can't be safely captured by a
# global pattern because sibling columns would mis-match (or the id has a county-unique name).
# Each is a SINGLE unambiguous candidate from recon/overnight/apn_field_candidates.md; consulted
# before APN_PATTERNS in normalize_county. (Coverage-checked at use: only honored if populated.)
APN_OVERRIDES = {
    "41045": "MapTaxlot",   # OR Malheur repointed layer: 'Taxlot' is the lot number WITHIN a map
                            # (969 distinct / 20,570 rows -> 95% dup APNs); MapTaxlot = map + lot
                            # (18,498 distinct; the rest are multi-part pieces) (2026-09-28)
    "26005": "mapping_id",  # MI Allegan (sole candidate)
    "26015": "Tparcel",     # MI Barry (sole candidate)
    "17101": "GISPin",      # IL Lawrence (GISPin is the id; GISPinNoDashes is its variant)
    "45003": "PARCEL_ASR",  # SC Aiken (sole id candidate)
    # Round 2 — live-probed sample values confirmed each is the real parcel id (2026-06-22):
    "26081": "PPN_TEXT",            # MI Kent: real Permanent Parcel Number. The tax_?parcel
                                    # pattern was false-matching TAXPARCEL_FLAG (a Yes/No flag)
                                    # -> 269k rows with apn='Yes'/'No' (dup-APN audit 2026-07-01)
    "18011": "stpin",               # IN Boone (formatted state PIN; newpin/oldpin are local pins)
    "26139": "FinalPIN",            # MI Ottawa (resolved PIN; ParentPIN/CondoPIN are not it)
    "27061": "parentpin",           # MN Itasca (sole candidate; verify uniqueness post-pull)
    "29005": "PARCEL_PID",          # MO Atchison (hierarchical GIS parcel id)
    "29013": "PARCEL_PID",          # MO Bates (== REALDATA_PARCEL)
    "29087": "PARCEL_PID",          # MO Holt (sole candidate)
    "42029": "PIN_ASMNT",           # PA Chester (assessment PIN; 194k parcels)
    "42015": "ParcelJoin",          # PA Bradford (16-digit PA control number)
    "45049": "Parcel_polygons_TMS", # SC Hampton (TMS parcel id)
    "45061": "Parcel_Polygons_PID", # SC Lee (TMS-style PID)
    "39113": "SDE.mc_parcel_polygon.TAXPINNO",  # OH Montgomery: the SDE.-prefixed column names
                                    # defeat every anchored APN pattern; TAXPINNO is 100% populated
                                    # ('B02 01107 0004' book-page-parcel), == WEB_CAMA.PARID where joined
    # ME statewide (MEGIS organized towns, all 16 counties): STATE_ID is the official statewide
    # parcel id (towngeocode_mapbooklot, e.g. '23020_05-018-000') — 100% populated, ~5% dup from
    # multi-polygon parcels, and contains the local MAP_BK_LOT a user would search.
    **{f"230{n:02d}": "STATE_ID" for n in range(1, 32, 2)},
    "22033": "ASSESSMENT_NUM",  # LA East Baton Rouge: the parish assessment number ('ID' is a row counter)
    "45019": "SDE.P_POLY_PARCEL.PID",  # SC Charleston: 10-digit PIN (SDE.-prefix defeats anchored patterns)
    "42091": "TAXPIN",  # PA Montgomery: geometry-side pin (100%) beats the joined table's copy (97%)
    "22055": "ASMNTNUM",  # LA Lafayette: bare parish assessment number (ASSMT_NUM = same with 'PAR-' prefix)
    "17167": "SOA.SANGIS.Parcel_Poly.PIN",  # IL Sangamon: SOA.-prefix defeats patterns; PIN 100%/0.1% dup
    "45041": "TMS",  # SC Florence: tax-map-sheet id 100%/0.07% dup
    # HI: the 9-digit TMK text is the parcel key everywhere (division-zone-section-plat-parcel)
    "15005": "tmk_txt", "15007": "tmk_txt", "15009": "tmk_txt",
    # VA tail (2026-07-03): localities publish the parcel id under local names; the bare "MAP"
    # style columns are mapbook PAGES (98%+ dup), the real id is the account/map-parcel composite.
    "51007": "ParcelID_Format", "51009": "MAP_NO", "51015": "parcels.ACCOUNT",
    "51025": "MapParcelID", "51091": "AMMAP_", "51115": "MapID", "51119": "TaxMapNo",
    "51139": "AccountJoin", "51147": "ACCOUNT", "51157": "MAP_NUMBER_1", "51179": "PRCLID",
    "51580": "TPID2Label", "51678": "MPGISPIN", "51683": "TAXMAP",
    # 2026-07-03 misc-backlog lane (verified nonnull/dup before adding; the rejected 'MAPNO'
    # style mapbook pages in Campbell VA / Clark KS stay honestly unmapped)
    "13313": "PARCEL_FUL", "42081": "DBO.Parcels.TXID_NMBR", "42103": "MAP_JOIN",
    "42105": "Map_Number", "45043": "SDE.PARCEL.TMS", "45081": "SDE.DBO.Parcels.TaxMapNumber",
    # 2026-07-03 degenerate-APN sweep (QA dup-baseline audit): the pattern pick landed on a
    # mapbook/grid component (MD 'PARCEL' = grid number, Charles had 906 distinct APNs on 72k
    # rows). Each replacement verified >=96% distinct in the raw parquet.
    # MD: ACCTID = SDAT account id (district+subdivision+account) — the id MD assessments key on.
    "24011": "ACCTID", "24015": "ACCTID", "24017": "ACCTID", "24019": "ACCTID",
    "24029": "ACCTID", "24035": "ACCTID", "24039": "ACCTID", "24045": "ACCTID",
    # 2026-09-14 degenerate-APN batch (project review 2026-09-06 finding #2): the 2026-07-17 MD
    # iMAP fill counties were ingested AFTER the 07-03 sweep and inherited the grid-number pick
    # (Howard: 1,169 distinct PARCEL on 100,460 rows). ACCTID verified 94-98% distinct and
    # 96-100% populated in every one of the nine raw parquets.
    "24001": "ACCTID", "24009": "ACCTID", "24013": "ACCTID", "24021": "ACCTID", "24023": "ACCTID",
    "24027": "ACCTID", "24037": "ACCTID", "24041": "ACCTID", "24047": "ACCTID",
    "24025": "FEATURE",      # MD Harford (own server): district+account tax id ('01000012'); the
                             # row's ASMT_LINK URL carries District=01&AccountNumber=000012. PARCEL = grid.
    "24043": "TaxAccNo",     # MD Washington (AGO Property_view/42): district+account ('01003739'),
                             # 97% non-null, one row per value in the live groupBy; Parcel = grid number.
    "13043": "PARCELLABEL",  # GA Candler (TaxParcel_26): map-block-parcel(-sub) label ('004    015 001'),
                             # 7,143 distinct of 7,151; PARCEL is the bare parcel component (280 distinct)
    "18113": "gisdata.SDE.Parcels.PIN",  # IN Noble: full state PIN ('57-01-01-100-001.000-013');
                                         # the joined surveyor table's ParcelNumber is a 3-digit stub
    "36095": "SBL",              # NY Schoharie: section-block-lot (ParcelNumb was a stub)
    "42127": "ControlNumber_1",  # PA Wayne: county control number '007594' (PARCELNO is a map stub)
    "42089": "PARID",            # PA Monroe: Tyler parcel id '01.28.1.10' (PARCEL is a 4-digit stub)
    "45083": "MAPNUMBER",        # SC Spartanburg: TMS '7-14-00-037.00' (PARCELNUMBER is the sheet number)
    "51117": "BigMapNum",        # VA Mecklenburg: full padded tax-map number (MAP-style cols are pages)
    # 2026-09-28 (source fan-out audit): apn was 100% NULL in both -- no pattern pick landed
    "36081": "SBL",              # NY Queens: section-block-lot, 324,164/324,164 live (Kings uses SBL too)
    "29189": "LOCATOR",          # MO St. Louis County: locator number '10D530140', 401,471/401,481 live
}
# Per-county situs COMPONENT overrides: sources that publish situs only as unprefixed component
# columns (_situs_components needs a situs_/site_ prefix to find them). Ordered street-address
# parts, joined with spaces at materialization (same path as detected components). Takes
# precedence over SITUS_PATTERNS -- PG MD's PROP_ADDR_SOURCE ('PMS', a source flag) was the
# pattern pick there, so the override must also SUPPRESS a bad single-column match.
# Per-county land-use field overrides: the pattern pick lands on a coded sibling when a
# descriptive column exists (Gwinnett PARCELTYPE 'P'/'R/W' vs PCDESC 'Residential SFR' --
# the keyword classifier needs words). Honored only when the column is present.
USE_OVERRIDES = {
    "13135": "PCDESC",
    "06073": "ASR_LANDUSE",  # San Diego assessor use code (98.4%); no USE_PATTERN matches it
}
SITUS_COMPONENT_OVERRIDES = {
    "24033": ["HOUSE_NUMBER", "HOUSE_SUFFIX", "STREET_DIRECTION", "STREET_NAME", "STREET_TYPE", "UNIT"],
    "24031": ["PREMISE_ADDR_HOUSENO", "PREMISE_ADDR_DIRECTION", "PREMISE_ADDR_STREET", "PREMISE_ADDR_STREET_TYPE"],
    "45019": ["SDE.CAMA.PROP_ST_NO", "SDE.CAMA.PROP_ST_NAME"],  # SC Charleston situs components
    "42091": ["LOCATION1"],  # PA Montgomery BOA: situs street line (LOCATION2 = unit/condo tail)
    "22055": ["PROPERTY_LOCATION"],  # LA Lafayette situs (98%)
    "51153": ["StreetNumber", "StreetName", "StreetType"],  # VA Prince William situs components
    "17167": ["SOA.SANGIS.Reg_ptinfo.PropStreet"],  # IL Sangamon situs street (80%)
    "45041": ["ADDR_SITE"],  # SC Florence situs (80%)
}
# Per-LAYER field overrides for shared statewide layers (keyed by a source_url substring, like
# ingest.STATEWIDE_SLICERS): their field names defeat the patterns and a GENERIC pattern would be
# unsafe elsewhere (e.g. 'ownerAdd'-style names are a name-overflow line in Skagit). Precedence:
# per-fips overrides > these > patterns. Each source is honored only when present and >= 2%
# populated, and the usual guards still apply (dollar plausibility, lot-area check, state codes).
# Spec suffixes: '|CSZ' = parse the 2-letter state out of a combined 'CITY, ST ZIP' string;
# '|ZIP' = extract the trailing 5-digit zip from such a string.
# Verified live 2026-09-28 (recon/source_fanout_2026-09-27.md step 3; full-county fill counts in
# recon/source_fanout_2026-09-27/remap_wins_*.csv).
LAYER_FIELD_OVERRIDES = {
    "Colorado_Public_Parcels": {  # 2.25M/2.60M ownerAdd filled statewide
        "mailing_address": "ownerAdd", "mail_city": "ownAddCty", "mail_state": "ownAddStt",
        "mail_zip": "ownAddZip", "land_use_desc": "landUseDsc"},
    "MS_Parcels_August_2024": {"mail_city": "MCITY1", "mail_state": "MSTATE1", "mail_zip": "MZIP1"},
    # MN: the owner address is a free-form 4-line BLOCK whose layout varies by county (Anoka:
    # street/city/state/zip on l1-l4; Dakota/Ramsey/...: street + 'City, MN 55403' on l3; Aitkin/Lake:
    # a name or c/o line on l1, street on l2). Parsed by _address_block. EMV = estimated market value.
    "plan_parcels_open": {
        "mail_block": "own_add_l1,own_add_l2,own_add_l3,own_add_l4",
        "land_value": "emv_land", "improvement_value": "emv_bldg"},
    "WV_Parcels": {"situs_address": "FullPhysicalAddress"},
    "L3Parcels_feature_service": {  # MA: last sale (incl. $1 nominal transfers) + residential area
        "sale_price": "LS_PRICE", "sale_date": "LS_DATE", "building_sqft": "RES_AREA"},
    # FL DOR statewide cadastral (NAL fields; the manifest records the DEP zip, the crosswalk the
    # AGOL layer): SALE_PRC1 = most recent sale in the current/prior roll year, 0 = none. No pattern
    # matches it. SALE_YR1 is a bare year with no month -> not mapped to sale_date (no invented dates).
    "Cadastral_Statewide.zip": {"sale_price": "SALE_PRC1"},
    "Florida_Statewide_Cadastral": {"sale_price": "SALE_PRC1"},
    # WI V12: PSTLADRESS is ONE combined postal string whose layout varies by county
    # ('street, CITY ST ZIP' in Dane; 'street CITY, ST ZIP' elsewhere), so the street can't be split
    # from the city reliably -> mailing_address stays unmapped; state + zip parse cleanly.
    "Wisconsin_Statewide_Parcels": {
        "situs_address": "SITEADRESS", "mail_state": "PSTLADRESS|CSZ", "mail_zip": "PSTLADRESS|ZIP"},
}


_CSZ_LINE = re.compile(r"^\s*(?P<city>[A-Za-z][A-Za-z .'\-]*?)\s*,?\s+(?P<st>[A-Za-z]{2})\s+"
                       r"(?P<zip>\d{5})(?:-?\d{4})?\s*$")


def _address_block(gdf, line_cols):
    """Parse a multi-line owner-address block -> (street, city, state, zip) Series. The city/state/zip
    line is the LAST line shaped 'City, ST 12345'; the street is the line just above it. Also handles
    the split layout street / city / 'ST' / '12345' (MN Anoka). Rows fitting neither stay null --
    a name or c/o line is never taken as the street."""
    lines = [gdf[c].astype("string").str.strip().replace("", pd.NA) for c in line_cols]
    street = pd.Series(pd.NA, index=gdf.index, dtype="string")
    city, st, zp = street.copy(), street.copy(), street.copy()
    done = pd.Series(False, index=gdf.index)
    for i in range(len(lines) - 1, 0, -1):          # the CSZ line needs a street line above it
        m = lines[i].str.extract(_CSZ_LINE)
        above = lines[i - 1]                         # nearest NON-EMPTY line above (Dakota: l2 blank)
        for j in range(i - 2, -1, -1):
            above = above.fillna(lines[j])
        hit = ~done & m["st"].notna() & above.notna()
        street, city = street.mask(hit, above), city.mask(hit, m["city"])
        st, zp = st.mask(hit, m["st"].str.upper()), zp.mask(hit, m["zip"])
        done |= hit
    if len(lines) >= 4:                             # split layout: l1 street, l2 city, l3 ST, l4 zip
        hit = (~done & lines[0].notna() & lines[1].notna()
               & lines[2].str.fullmatch(r"[A-Za-z]{2}").fillna(False)
               & lines[3].str.fullmatch(r"\d{5}(-?\d{4})?").fillna(False))
        street, city = street.mask(hit, lines[0]), city.mask(hit, lines[1])
        st, zp = st.mask(hit, lines[2].str.upper()), zp.mask(hit, lines[3].str[:5])
    return street, city, st, zp


def _layer_overrides(source_url, cols, gdf):
    """{attribute: spec} for the shared statewide layer this county came from, keeping only specs
    whose source column exists (any casing -> the source's casing) and is >= 2% populated."""
    lower = {c.lower(): c for c in cols}
    out = {}
    for sub, spec in LAYER_FIELD_OVERRIDES.items():
        if sub not in (source_url or ""):
            continue
        for attr, src in spec.items():
            if attr == "mail_block":                 # all lines must exist; parsed later
                names = [lower.get(n.lower()) for n in src.split(",")]
                if all(names):
                    out[attr] = names
                continue
            name, _, mode = src.partition("|")
            col = lower.get(name.lower())
            if not col:
                continue
            filled = float((gdf[col].notna() & (gdf[col].astype(str).str.strip() != "")).mean())
            if filled >= 0.02:
                out[attr] = f"{col}|{mode}" if mode else col
    return out


SITUS_PATTERNS = [r"situs_?(addr|address|line)?$", r"situs_?line_?1", r"^situs_?stre$",
                  r"^situs_?addr",  # Benton OR Situs_Addr1
                  r"^situs_?add",   # CO statewide 'situsAdd' (abbrev, no trailing 'r')
                  r"^site_?addr", r"^site_?add",  # NC OneMap 'siteadd'
                  r"^adr_?label$",  # AR GeoStor pre-composed address label
                  r"^full_?add", r"parcel_add",
                  r"^prop(erty)?_?addr", r"^phys", r"^phy_?addr",  # FL NAL PHY_ADDR1
                  r"^dlgf_prop_address$",  # IN IndianaMap statewide situs line (98.9%; $ excludes
                                           # the _city/_state/_zip component columns). dlgf = IN-only.
                  r"^location_?(addr)?$", r"^st_?address",
                  r"^address$",  # bare ADDRESS on assessor layers (TN statewide) = situs
                  r"^street_?addr"]  # last/gap-fill: NH 'StreetAddress' situs (can't override an
                                     # existing pick; only fills states with no other situs match)
MAIL_PATTERNS = [r"mail.*(addr|line|1)", r"^mail.*ad", r"^m_address", r"^mailing", r"^own(er)?_?addr",
                 # MD SDAT family (all 17 MD counties + Harford/Washington exports): OWNADD1 / OwnAdd1 /
                 # OWN_AD_1 lack the 'r' the pattern above needs (2026-09-14: 17 cos had mailing=NULL)
                 r"^own_?add_?1$", r"^own_?ad_?1$",
                 r"taxpayer_?addr", r"(holder|owner|taxpayer).*addr(ess)?_?1?$",
                 r"^billing_?1?$", r"^address_?1?$"]
# Per-county mailing-street overrides for sources whose owner-address field naming defeats the
# patterns above (e.g. abbreviated "OwnerAdd1" without the 'r', or a multi-line bottom-aligned block).
# Value is a normalize coalesce spec ("colA|colB|colC" = first non-null per row, left to right) so a
# 3-line block resolves to its street line. Validated per county (Item 2b, see CORROBORATION_SCOPE.md).
# A GENERIC "OwnerAdd1" pattern was rejected: Skagit's Add1 is a NAME-overflow line, so a blind pick
# would write names into mailing_address -> per-county overrides only.
MAIL_OVERRIDES = {
    "53057": "OwnerAdd3|OwnerAdd2|OwnerAdd1",  # Skagit WA: bottom-aligned block, street = last line
    "42071": "OWNERADD1",                       # Lancaster PA: street is line 1
    "39113": "SDE.WEB_CAMA.MAILING_ADDR1",      # OH Montgomery: unanchored mail.*1 pattern hits
                                                # MAILING_NAME1 (an owner NAME) first; ADDR1 is the street
    # '+' = space-joined components (not coalesce): Charleston splits the mailing street line into
    # no/name/type columns; the pattern pick (MAIL_2ND_ADDR) is 9.7%-populated suite/PO fragments
    "45019": "SDE.CAMA.MAIL_ST_NO+SDE.CAMA.MAIL_ST_NAME+SDE.CAMA.MAIL_ST_TYPE",
    "42091": "ADDR1",  # PA Montgomery BOA table: ADDR1=owner mailing street (ADDR3 is its CSZ line)
    # HI Maui/Kalawao joined layer: STREET/CITY/STATE/ZIP1 are the OWNER mailing block
    "15005": "STREET", "15009": "STREET",
}
MAIL_EXCLUDE = r"(city|state|zip|_ci|_st)\d?$"   # MAILING_STATE/MAILING_CI are not the street line
# owner's MAILING state (absentee signal). A dedicated 2-letter field (mail.*state / owner_state),
# else parsed from a combined city/state/zip field (Daviess KY 'city_st_zip'). Mail/owner-prefixed
# only -> never grabs the PARCEL's state or situs/prop state.
MAIL_STATE_PATTERNS = [r"mail.*state", r"^m_?state$", r"^own(er)?_?state$", r"taxpayer.*state",
                       r"billing.*state", r"^mailing_?st$", r"^owner_?st$", r"^ml_?state$",
                       r"^mail_?stat$"]  # Gwinnett MAILSTAT (2-letter states, 99%)
MAIL_CSZ_PATTERNS = [r"city_?st_?zip", r"^csz$", r"mail.*csz", r"own.*csz", r"city_?state_?zip",
                     r"mail_?city_?st", r"^cityst$", r"^last_?line$", r"addr_?last"]
US_STATE_ABBR = {"AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL", "IN",
                 "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV",
                 "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN",
                 "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY", "DC"}
USE_PATTERNS = [r"^land_?use", r"^landuse", r"^use_?code", r"^primary_use", r"^use_?desc",
                r"^par_?use_?desc$",   # NC OneMap parusedesc (72.3% non-blank, beats parusecode)
                r"^lu_?code$",         # Hamilton TN assessor extract
                r"^prop(erty)?_?(use|class)", r"^usecd", r"^dorcode",
                r"^dor_?uc$",          # FL NAL DOR use code
                r"^stat_?land_?use$",  # TX stratmap state land-use code
                r"^loc_?land_?use$",
                r"^parcel_?type$",     # AR GeoStor RI/AV/CI classification
                r"^prop_?type$",       # MT cadastral property type
                r"^dlgf_prop_class"]   # IN IndianaMap statewide layer (dlgf_prop_class_code, DLGF
                                       # Code List 1); 'dlgf' token is IN-only -> no cross-state hit
# OH-only land-use fields, appended to USE_PATTERNS for OH counties ONLY (in normalize_county) so
# they can never affect another state's land_use pick: Fairfield/Richland 'LUC', Montgomery
# 'SDE.WEB_CAMA.LUC', Gallia 'PPClassNumber'. (Summit 'usecd' is already covered above.)
OH_USE_PATTERNS = [r"(^|[._])luc$", r"^ppclass"]
ACRE_PATTERNS = [r"gis_?acre", r"^acre(s|age)?$", r"^calc.*acre", r"legal_?acre", r"acre",
                 r"^landgross"]  # WY LANDGROSSA
# land square-footage columns convertible to acres when no acre column exists (FL LND_SQFOOT)
SQFT_TO_ACRE_PATTERNS = [r"^lnd_?sq_?foot$", r"^land_?sq_?ft$", r"^sq_?ft_?lot$"]
VALUE_PATTERNS = [r"^total_?(val|value|av)", r"^totval", r"^total", r"^rmv_?tot(al)?",  # OR Real Market Value
                  r"(mrk|mkt|market)\w{0,4}_?tot",
                  r"tot_?val", r"ass\w{0,6}_?tot", r"appraised", r"^market$",
                  r"market_?(val|total)", r"^assess", r"assessed_?val", r"^av_?total", r"^emv",
                  r"^mkt", r"total_?assess"]
# Same non-dollar/non-id name rejections as resolve.py's _best_value, plus URL/link columns
# (Skamania's Assess_URL would otherwise match ^assess) and tax-AMOUNT columns (Malheur's
# declared TAXES is the annual bill, not a value)
BAD_VALUE = ("date", "year", "yr", "_id", "objectid", "zip", "fips", "yyyymmdd", "_dt", "instrmnt",
             "acre", "url", "link", "hyper", "taxes", "tax_amt", "taxamt", "total_tax", "tax_total",
             "net_tax", "_tax_", "tax_bill",  # tax-AMOUNT fields (MN total_tax; IL tax_billed)
             # sale prices and areas pass the dollar-plausibility check but are not values:
             # Haakon/Turner SD TOTAL_LIVING_AREA matched ^total (median 1,340 'dollars')
             "sale", "price", "living", "sqft", "sq_ft", "_area")
# The subset of BAD_VALUE that flags a tax AMOUNT (dollars owed) vs a value. A column naming itself
# "taxable"/"txbl" is a taxable VALUE, not an amount -- exempt it from these so total_tax doesn't
# substring-reject TOTAL_TAXABLE_VALUE (Grant MN, verified 84% populated median $209k -> was 0%).
_TAX_AMOUNT = ("taxes", "tax_amt", "taxamt", "total_tax", "tax_total", "net_tax", "_tax_", "tax_bill")
# Strong VALUE signals: a column flagged ONLY by a tax-AMOUNT token but that also names itself a value
# (assessed/appraised/market/taxable/...) is a value in a tax context -- 'assess_total_taxyr' (Lake IL,
# the substring is total_tax->yr) and 'total_taxable_value' (Grant MN) -- not a dollars-owed amount.
_VALUE_HINT = ("assess", "apprais", "market", "mkt", "rmv", "emv", "fcv", "sev", "value",
               "taxable", "txbl", "just")


def _bad_value(name):
    n = name.lower()
    hits = [b for b in BAD_VALUE if b in n]
    if not hits:
        return False
    if all(h in _TAX_AMOUNT for h in hits) and any(v in n for v in _VALUE_HINT):
        return False  # value field named in a tax context, not a tax amount
    return True


# Component-qualified value columns (land-only, improvements-only, taxable, current-use):
# never the headline assessed_value when an unqualified total exists.
QUALIFIED = ("land", "lnd", "imp", "bldg", "build", "timber", "txbl", "taxable", "_cu", "cu_")

# --- Enrichment attributes (locked set 2026-06): more of what sources already expose. Ranked
# name patterns, seeded from the field-discovery probe (output/source_fields_sample.csv) and
# proven on RI Providence. _pick is coverage-aware so an empty well-named column never wins.
MAIL_CITY_PATTERNS = [r"^mail.*city", r"^own(er)?_?city", r"^m_?city$", r"mailing_?city",
                      r"taxpayer.*city", r"^ml_?city$"]
MAIL_ZIP_PATTERNS = [r"^mail.*zip", r"^own(er)?_?zip", r"^m_?zip$", r"mailing_?zip",
                     r"taxpayer.*zip", r"^ml_?zip"]
YEAR_BUILT_PATTERNS = [r"^year_?built", r"^yr_?blt$", r"yearblt", r"act(ual)?_?year_?built",
                       r"bldg_?yr_?blt", r"eff(ective)?_?year_?built", r"yr_?built", r"yr_?blt"]
# building_sqft is the only enrichment with NO data-level guard, so its generic sqft fallbacks
# (sq_ft$) are paired with SQFT_DECOY in normalize_county to drop price/lot/land/garage sqft fields.
SQFT_PATTERNS = [r"bldg_?sq_?ft$",
                 r"sq_?ft_?liv",  # NYS Tax Parcels SQFT_LIVING (its bare SQ_FT is the LOT size)
                 r"^sq_?ft$", r"^res_?sqft", r"living_?area", r"^total_?area$",
                 r"heated_?area", r"finished_?area", r"est_?sqft", r"gross_?area", r"bldg_?sf$",
                 r"struct.*sq", r"fin.*sq_?ft", r"main_?sq", r"size_?sq", r"sq_?ft$", r"sq_?feet$",
                 r"lvg_?ar",  # LVG abbreviation: San Diego TOTAL_LVG_AREA, FL DOR TOT_LVG_AR
                 r"^sqft_?strc$",     # MD SDAT: structure square feet
                 r"^struct_?area$"]   # MD Washington: StructArea
# usable_sq: San Diego USABLE_SQ_FEET is usable LAND area (~= acreage*43560), not building
# geom/gis_sq/parcel_area/shape: GIS-computed POLYGON area (Kern GEOM_SQFT, Monterey GIS_SQFT,
# Merced GIS_SQ_FT, Colusa/Inyo PARCEL_AREA_SQFT -- 99-100% equal to acreage*43560, 2026-09-28)
SQFT_DECOY = (r"per_?sq|prc|price|val.*sq|sq.*val|lot_?sq|land.*sq|garage|carport|acre|usable_?sq"
              r"|geom|gis_?sq|parcel_?area|shape")
# Data-level lot-area guard for the building_sqft pick (names can't catch a bare 'SQ_FT' that holds
# the lot size). Measured 2026-09-28 on live NYS samples, share of rows within 10% of acreage*43560:
# lot-size SQ_FT 73-96% (Kings/Bronx/NewYork/Albany/Erie/Richmond); real SQFT_LIVING 0-16%.
LOT_AREA_TOL = 0.10
LOT_AREA_MAX_SHARE = 0.50


def _lot_area_share(sqft, acres, sample=20000):
    """Share of rows whose 'building' sqft equals the parcel's own area (acres*43560) within
    LOT_AREA_TOL. None when too few rows carry both to judge."""
    both = pd.DataFrame({"s": pd.to_numeric(sqft, errors="coerce"),
                         "a": pd.to_numeric(acres, errors="coerce")}).dropna()
    both = both[(both.s > 0) & (both.a > 0)]
    if len(both) < 50:
        return None
    if len(both) > sample:
        both = both.sample(sample, random_state=0)
    ratio = both.s / (both.a * 43560)
    return float(((ratio - 1).abs() <= LOT_AREA_TOL).mean())
# land_value / improvement_value / sale_price are gated by _plausible_dollars, so broad name
# patterns are safe -- a non-dollar decoy is rejected on the data.
LANDVAL_PATTERNS = [r"land_?val", r"lnd_?val", r"land_?av$", r"land_?mkt", r"land_?assess",
                    r"app?r?_?land_?val", r"asst_?land", r"^av_?land",
                    r"^nfm_?lnd_?vl$"]  # MD SDAT: new full-market land value
IMPVAL_PATTERNS = [r"^imp_?val", r"impvalue", r"improvement_?val", r"bldg_?val", r"build(ing)?_?val",
                   r"struct_?val", r"app?r?_?imp", r"asst_?imp", r"^av_?imp", r"^improv",
                   r"^nfm_?imp_?vl$",   # MD SDAT: new full-market improvement value
                   r"^cur_?imp_?val?$"]  # MD Harford CUR_IMP_VA / Washington CurImpVal
SALE_PRICE_PATTERNS = [r"sale_?price", r"saleprice", r"sale_?amt", r"sale_?amount", r"^amt_?sale",
                       r"sales_?price", r"last_?sale.*pri", r"sales?_?pr$",
                       r"^considr?_?1$",   # MD SDAT: consideration on the most recent transfer
                       r"^con_?amt$",      # MD Harford
                       r"^ss1_?consid$"]   # MD Washington (sale slot 1)
# Nominal consideration is not a market price: $1 / $10 / $100 deeds (quitclaims, family and
# corporate transfers) were 13.3% of all positive sale prices nationally (1.45M rows, scan
# 2026-10-02; NJ/PA/SC heaviest; MA Middlesex 33%, FL 35-66%). User decision 2026-10-02: a
# sale_price <= this is stored as NULL. The sale_date is kept -- the transfer did happen.
NOMINAL_SALE_MAX = 100
SALE_DATE_PATTERNS = [r"sale_?date", r"saledate", r"^deed_?date", r"sales_?date", r"date_?sold",
                      r"last_?sale_?date", r"trans.*date", r"^rec(ord)?_?date",
                      r"^tra_?date$"]  # MD SDAT: transfer date
ZONING_PATTERNS = [r"^zoning", r"^zone$", r"^zone_?code", r"^zone_?desc", r"zoning_?district",
                   r"city_?zoning", r"^zone_?class"]
USE_DESC_PATTERNS = [r"use.*desc", r"class_?desc", r"prop(erty)?_?class$", r"use_?class\d?$",
                     r"major_?class", r"prop(erty)?_?sub_?class", r"use_?code_?desc", r"land_?use_?desc",
                     r"land_?use_?de", r"land_?use_?te", r"^prop_?type$"]

SCHEMA = ["state", "county_fips", "county_name", "apn", "owner",
          "situs_address", "mailing_address", "mail_state", "mail_city", "mail_zip",
          "assessed_value", "land_value", "improvement_value",
          "land_use", "land_use_desc", "zoning", "acreage",
          "year_built", "building_sqft", "sale_price", "sale_date",
          "source_url", "source_vintage", "retrieved_at", "geometry"] + ROLE_COLUMNS

# Source-data placeholder strings that masquerade as owners. Rewritten to bracketed
# sentinels so every suppression marker in the dataset is uniformly excludable with
# owner NOT LIKE '[%' (research.py does this in rankings). Found by full-distribution
# audit (2026-06-10): Jefferson WA masks all natural persons behind one literal string.
OWNER_SENTINEL_REWRITES = {
    "53031": {"INDIVIDUAL OWNER": "[INDIVIDUAL OWNER - natural-person placeholder (Jefferson WA, RCW privacy)]"},
}
# Universal lawful-redaction placeholders (any county): the RAW owner is itself a placeholder, so
# bracket it so it's uniformly excludable (owner NOT LIKE '[%') and passes QA's raw-owner sentinel
# check. Matched on upper(trim(owner)). (Per-county rewrites above still win for specific phrasings.)
GLOBAL_OWNER_SENTINELS = {
    "NOT PUBLISHED": "[NOT PUBLISHED - source placeholder]",
    "OWNER NOT PUBLISHED": "[NOT PUBLISHED - source placeholder]",
    "SUPPRESSED": "[OWNER SUPPRESSED - source placeholder]",
    "OWNER SUPPRESSED": "[OWNER SUPPRESSED - source placeholder]",
}


def _pick(columns, patterns, exclude=(), gdf=None):
    """Highest-ranked pattern-matching column. When gdf is given, selection is
    COVERAGE-AWARE: take the best-ranked candidate that's >=50% populated, else the
    most-populated candidate (>=2%) — a well-named but empty column (Klamath's MAP_TAXLOT
    sitting next to the populated MAPTAXLOT_) must never win on name alone."""
    cands = []
    for pat in patterns:
        for c in columns:
            if c in exclude or c in cands:
                continue
            if re.search(pat, c, re.IGNORECASE):
                cands.append(c)
    if not cands:
        return None
    if gdf is None:
        return cands[0]
    cov = {c: float((gdf[c].notna() & (gdf[c].astype(str).str.strip() != "")).mean()) for c in cands}
    for c in cands:
        if cov[c] >= 0.5:
            return c
    best = max(cands, key=lambda c: cov[c])
    return best if cov[best] >= 0.02 else None


def _to_num(s):
    """Numeric coercion tolerant of FORMATTED source strings -- '99,400', '$1,234', ' 842,300 '
    (Maricopa AZ FCV_CUR and other comma-thousands value fields). Strips currency/grouping/whitespace
    before pd.to_numeric so a plain thousands-comma no longer nulls a real dollar value to 0. Numeric
    columns pass through unchanged (the string path is skipped)."""
    if s.dtype == object or pd.api.types.is_string_dtype(s):
        s = (s.astype(str).str.replace(r"[\$,\s]", "", regex=True)
             .replace({"": None, "nan": None, "None": None}))
    return pd.to_numeric(s, errors="coerce")


def _plausible_dollars(s):
    """Data-level sanity: a value column must LOOK like dollars. Catches well-named decoys
    a name rule can't (Yakima's ASSESSOR_N = 11-digit parcel numbers that parse as numbers)."""
    v = _to_num(s).dropna()
    v = v[v > 0]
    if len(v) == 0:
        return False
    return v.median() < 5e7 and (v >= 1e9).mean() < 0.5  # median parcel isn't $50M+; IDs are huge


def _value_plan(gdf, columns, declared):
    """How to derive assessed_value: ('col', name), ('sum', [land, improvement]) when the
    county publishes only the components (the Cowlitz/Asotin class), or (None, None).
    Every candidate must pass the dollar-plausibility check on the actual data."""
    if declared and str(declared).strip().upper() == "NONE":
        return None, None  # explicit "source has no usable assessed-value field" override
                           # (county_methods.csv) — stops auto-detect picking acreage/tax/sale-price
    if declared and "+" in str(declared):
        # explicit multi-component sum (IL EAV counties publish farm/non-farm x land/building as
        # four columns, none name-matching the value patterns; total = their sum). Auditable in
        # the crosswalk; honored only when every named part is a real column.
        parts = [p.strip() for p in str(declared).split("+") if p.strip()]
        if parts and all(p in columns for p in parts):
            return "sum", parts
        # a part is missing (truncated/renamed source) -> fall through to auto-detect, don't crash
    def pick_valid(cands):
        tried = set()
        while True:
            col = _pick([c for c in cands if c not in tried], VALUE_PATTERNS)
            if not col:
                return None
            if _plausible_dollars(gdf[col]):
                return col
            tried.add(col)
    # reject acreage columns whose name ends in an acreage token (TOTAL_AC, CALC_ACRES): the
    # substring "acre" in BAD_VALUE misses the "_AC"/"_ACS" abbreviations, and ^total then wrongly
    # matches TOTAL_AC as a value (MS Montgomery/Washington/Winston, where TOTVAL is all-zero).
    ok = [c for c in columns if not _bad_value(c)
          and not re.search(r"(^|_)ac(re)?s?$", c.lower())]
    valish = [c for c in ok if re.search(r"val|assess|market|mkt|rmv|emv|^av|_av$|_av_", c, re.IGNORECASE)]
    # An EXPLICITLY declared value_field is the operator's vouched choice -- honor it as long as it holds
    # plausible dollars. BAD_VALUE is a NAME heuristic for AUTO-detection only; it must not veto an
    # explicit declaration whose name coincidentally contains a tax/year substring (Lake IL
    # 'assess_total_taxyr' trips both 'total_tax' and 'yr'; Grant MN 'TOTAL_TAXABLE_VALUE').
    if declared and declared in columns and _plausible_dollars(gdf[declared]):
        # a LAND-only declared field (Baker OR's AVLAND) gets its improvement counterpart summed in
        if re.search(r"land|lnd", declared, re.IGNORECASE):
            imp = next((c for c in valish if re.search(r"imp|bldg|build", c, re.IGNORECASE)), None)
            if imp:
                return "sum", [declared, imp]
        return "col", declared
    unqualified = [c for c in ok if not any(q in c.lower() for q in QUALIFIED)]
    col = pick_valid(unqualified)
    if col:
        return "col", col
    land = next((c for c in valish if re.search(r"land|lnd", c, re.IGNORECASE)), None)
    imp = next((c for c in valish if re.search(r"imp|bldg|build", c, re.IGNORECASE)), None)
    if land and imp:
        return "sum", [land, imp]
    col = pick_valid(ok)  # last resort: a qualified column beats nothing
    return ("col", col) if col else (None, None)


def _situs_components(cols):
    """Counties like Cowlitz/Whatcom publish situs only as components (SITUS_STREET_NUMBER,
    situs_num/situs_street/...). Returns the component columns in street-address order, or None.
    Requires at least a street-name component plus one other part."""
    for pre in ("situs_street_", "situs_", "site_"):
        group = [c for c in cols if c.lower().startswith(pre)
                 and not re.search(r"(city|state|zip)", c, re.IGNORECASE)]
        if len(group) < 2:
            continue

        def find(*subs):
            for s in subs:  # exact remainder first: situs_street beats situs_street_prefix
                for c in group:
                    if c.lower()[len(pre):] == s:
                        return c
            for s in subs:
                for c in group:
                    if s in c.lower()[len(pre):]:
                        return c
        name = find("name", "street", "strt")
        if not name:
            continue
        seq = [find("number", "num", "house"), find("predir", "prefix", "pre", "direction", "dir"),
               name, find("type", "typ", "suffix", "sfx"), find("postd"), find("unit")]
        seq = [c for c in dict.fromkeys(c for c in seq if c)]
        if len(seq) >= 2:
            return seq
    return None


def _clean_str(s):
    """Column -> trimmed string series; integer-like numerics lose the trailing '.0'.
    Values with no letter or digit ('   , ,' — Terrell TX blank-padded composites)
    become "" so the callers' .replace("", None) nulls them instead of counting
    punctuation junk as populated."""
    if pd.api.types.is_numeric_dtype(s):
        return s.apply(lambda v: "" if pd.isna(v)
                       else str(int(v)) if float(v).is_integer() else str(v))
    s = s.fillna("").astype(str).str.split().str.join(" ")
    return s.where(s.str.contains(r"[A-Za-z0-9]", regex=True), "")


def _owner_series(gdf, owner_fields):
    """Vectorized version of resolve.py's _owner_from: collapse padding, dedupe, ' / ' join."""
    parts = []
    for fld in (owner_fields or "").split("|"):
        if fld in gdf.columns:
            s = gdf[fld].fillna("").astype(str).str.split().str.join(" ")
            parts.append(s)
    if not parts:
        return pd.Series([None] * len(gdf), index=gdf.index)

    def combine(vals):
        seen = []
        for v in vals:
            if v and v.lower() not in ("none", "null") and v not in seen:
                seen.append(v)
        return " / ".join(seen) or None
    return pd.concat(parts, axis=1).apply(lambda r: combine(list(r)), axis=1)


_STATE_WINDOWS = {}


def _state_window(state2):
    """Generous lon/lat window per state (union of its county bboxes +0.5 deg), for
    catching COMPACT displaced junk geometries that evade the 5-degree-span rule
    (QA finding 2026-06-12: El Paso TX parcels rendered in the Pacific)."""
    if not _STATE_WINDOWS:
        with open(os.path.join(ROOT, "data", "us_counties.geojson"), encoding="utf-8") as f:
            cj = json.load(f)
        for feat in cj["features"]:
            st = feat["properties"]["STATE"]
            def walk(c):
                if isinstance(c[0], (int, float)):
                    yield c
                else:
                    for x in c:
                        yield from walk(x)
            xs, ys = zip(*walk(feat["geometry"]["coordinates"]))
            w = _STATE_WINDOWS.setdefault(st, [180, 90, -180, -90])
            w[0], w[1] = min(w[0], min(xs)), min(w[1], min(ys))
            w[2], w[3] = max(w[2], max(xs)), max(w[3], max(ys))
    return _STATE_WINDOWS.get(state2)


# States whose statewide layer exposes NO lastEditDate, but each parcel carries a usable
# date/tax-year field -> derive source_vintage per-row (currency) instead of leaving it NULL.
# kind 'year' = integer tax year (kept only 1990-2030, so ID/MS '0' sentinels stay NULL);
# 'date' = an esri Date (epoch-ms / datetime / ISO) -> 'YYYY-MM-DD' (kept only 2000-2027, so the
# NC/AR year-9999 and pre-2000 null-sentinels stay NULL). Fields probed live 2026-06-22.
VINTAGE_DATE_FIELDS = {
    "NM": ("TaxYear", "year"), "MT": ("TaxYear", "year"), "ID": ("taxyear", "year"),
    "MS": ("TAXYEAR", "year"), "TX": ("tax_year", "year"), "DC": ("EFFECTIVETAXYEAR", "year"),
    "NC": ("revisedate", "date"),  # sourcedate is empty in the data; revisedate (epoch-ms) is 100% filled
    "AR": ("sourcedate", "date"),
}


def _derive_vintage(gdf, cols, state):
    """Per-row source_vintage from a tax-year/date column when the layer has no lastEditDate.
    Returns a string Series ('2025 (tax year)' / '2025-10-10') aligned to gdf.index, NA where the
    source value is missing/implausible — junk sentinels stay NULL, never falsely dated."""
    spec = VINTAGE_DATE_FIELDS.get(state)
    if not spec:
        return None
    field, kind = spec
    col = next((c for c in cols if c.lower() == field.lower()), None)
    if not col:
        return None
    if kind == "year":
        y = pd.to_numeric(gdf[col], errors="coerce").where(lambda s: (s >= 1990) & (s <= 2030))
        return (y.dropna().astype(int).astype(str) + " (tax year)").reindex(gdf.index)
    raw = gdf[col]  # esri Date: numeric epoch-ms, or datetime64, or ISO string
    if pd.api.types.is_numeric_dtype(raw):
        dt = pd.to_datetime(pd.to_numeric(raw, errors="coerce"), unit="ms", errors="coerce", utc=True)
    else:
        dt = pd.to_datetime(raw, errors="coerce", utc=True)
    lo, hi = pd.Timestamp("2000-01-01", tz="UTC"), pd.Timestamp("2027-12-31", tz="UTC")
    return dt.where((dt >= lo) & (dt <= hi)).dt.strftime("%Y-%m-%d").reindex(gdf.index)


def normalize_county(entry, out_dir):
    raw_path = os.path.join(ROOT, entry["file"])
    gdf = gpd.read_parquet(raw_path)
    gdf, GRAIN_DROPS[entry["fips"]] = grain_collapse(gdf, entry["fips"])
    entry = mapping_entry(entry, gdf)
    cols = [c for c in gdf.columns if c != "geometry"]

    owner_cols = [f for f in (entry["owner_fields"] or "").split("|") if f in cols]
    vkind, vsrc = _value_plan(gdf, cols, entry.get("value_field") or None)
    lov = _layer_overrides(entry.get("source_url"), cols, gdf)  # shared statewide layer field map
    situs = lov.get("situs_address") or _pick(cols, SITUS_PATTERNS, gdf=gdf)
    sov = SITUS_COMPONENT_OVERRIDES.get(entry["fips"])
    if sov and all(p in cols for p in sov):
        situs, situs_parts = None, sov
    else:
        situs_parts = None if situs else _situs_components(cols)
    apn_cands = [c for c in cols if not re.search(APN_EXCLUDE, c, re.IGNORECASE)]
    apn_pick = None
    ov = APN_OVERRIDES.get(entry["fips"])
    if ov:
        match = next((c for c in cols if c.lower() == ov.lower()), None)
        if match and float((gdf[match].notna() & (gdf[match].astype(str).str.strip() != "")).mean()) >= 0.02:
            apn_pick = match
    fmap = {
        "owner": "|".join(owner_cols) or None,
        "apn": apn_pick or _pick(apn_cands, APN_PATTERNS, gdf=gdf),
        "situs_address": situs or ("+".join(situs_parts) if situs_parts else None),
        "assessed_value": "+".join(vsrc) if vkind == "sum" else vsrc,
        "land_use": (USE_OVERRIDES.get(entry["fips"]) if USE_OVERRIDES.get(entry["fips"]) in cols
                     else _pick(cols, USE_PATTERNS + (OH_USE_PATTERNS if entry["state"] == "OH" else []), gdf=gdf)),
        # 'per' excludes rate columns (Island's MarketPerAcre is $/acre, not acreage)
        "acreage": _pick([c for c in cols if "per" not in c.lower()], ACRE_PATTERNS, gdf=gdf),
    }
    if not fmap["acreage"]:
        sqft = _pick(cols, SQFT_TO_ACRE_PATTERNS, gdf=gdf)
        if sqft:
            fmap["acreage"] = f"{sqft}/43560"
    # mailing must not re-grab the situs column (San Juan: SitusAddr vs Address_1), and
    # city/state/zip components (Yakima MAILING_STATE) are not the street line
    mail_cands = [c for c in cols if not re.search(MAIL_EXCLUDE, c, re.IGNORECASE)]
    mov = MAIL_OVERRIDES.get(entry["fips"])
    mov_sep = "+" if (mov and "+" in mov) else "|"  # '+' = space-joined components, '|' = coalesce
    if mov and all(any(c.lower() == p.lower() for c in cols) for p in mov.split(mov_sep)):
        # map override col-names to the source's actual casing, keep the spec order
        fmap["mailing_address"] = mov_sep.join(next(c for c in cols if c.lower() == p.lower())
                                               for p in mov.split(mov_sep))
    elif lov.get("mail_block"):
        fmap["mailing_address"] = None  # filled from the parsed block after the enrichment picks
    elif lov.get("mailing_address"):
        fmap["mailing_address"] = lov["mailing_address"]
    else:
        fmap["mailing_address"] = _pick(mail_cands, MAIL_PATTERNS, exclude={situs}, gdf=gdf)
    # bare "Address_1"-style mailing is only trustworthy when situs lives in its own column
    if fmap["mailing_address"] and not fmap["situs_address"] \
            and re.match(r"^address_?1?$", fmap["mailing_address"], re.IGNORECASE):
        fmap["mailing_address"] = None

    out = gpd.GeoDataFrame(index=gdf.index, geometry=gdf.geometry, crs=gdf.crs)
    out["state"] = entry["state"]
    out["county_fips"] = entry["fips"]
    out["county_name"] = entry["county"]
    out["owner"] = _owner_series(gdf, fmap["owner"])
    for plain, sentinel in OWNER_SENTINEL_REWRITES.get(entry["fips"], {}).items():
        out["owner"] = out["owner"].replace(plain, sentinel)
    _up = out["owner"].fillna("").astype(str).str.strip().str.upper()
    for plain, sentinel in GLOBAL_OWNER_SENTINELS.items():
        out.loc[_up == plain, "owner"] = sentinel
    # NC: coalesce(parusedesc, parusedsc2) recovers 189k blank-primary parcels (72.3% -> 75.5%,
    # measured live on NC OneMap 2026-06-12); secondary-desc column only, codes are county-local
    if fmap["land_use"] and fmap["land_use"].lower() == "parusedesc" \
            and any(c.lower() == "parusedsc2" for c in cols):
        second = next(c for c in cols if c.lower() == "parusedsc2")
        fmap["land_use"] = f"{fmap['land_use']}|{second}"
    for col in ("apn", "mailing_address", "land_use"):
        src = fmap[col]
        if src and "+" in src:  # plus = space-joined components (Charleston MAIL_ST_NO+NAME+TYPE)
            joined = pd.concat([_clean_str(gdf[c]) for c in src.split("+")], axis=1).agg(" ".join, axis=1)
            out[col] = joined.str.split().str.join(" ").replace("", None)
        elif src and "|" in src:  # pipe = coalesce left-to-right
            parts = [_clean_str(gdf[c]).replace("", None) for c in src.split("|")]
            s = parts[0]
            for nxt in parts[1:]:
                s = s.fillna(nxt)
            out[col] = s
        else:
            out[col] = _clean_str(gdf[src]).replace("", None) if src else None
    if situs:
        out["situs_address"] = _clean_str(gdf[situs]).replace("", None)
    elif situs_parts:
        joined = pd.concat([_clean_str(gdf[c]) for c in situs_parts], axis=1).agg(" ".join, axis=1)
        out["situs_address"] = joined.str.split().str.join(" ").replace("", None)
    else:
        out["situs_address"] = None
    # mail_state: owner's mailing state (absentee signal). dedicated 2-letter field preferred, else
    # parse from a combined city/state/zip field; validated against US state codes (junk -> null).
    lms = lov.get("mail_state") or ""
    if lms.endswith("|CSZ"):
        ms_col, csz_col = None, lms[:-len("|CSZ")]
    else:
        ms_col = lms or _pick(cols, MAIL_STATE_PATTERNS, gdf=gdf)
        csz_col = None if ms_col else _pick(cols, MAIL_CSZ_PATTERNS, gdf=gdf)
    if ms_col:
        ms = _clean_str(gdf[ms_col]).str.upper().str.strip()
        fmap["mail_state"] = ms_col
    elif csz_col:
        ms = _clean_str(gdf[csz_col]).str.upper().str.extract(r"([A-Z]{2})\s+\d{5}", expand=False)
        fmap["mail_state"] = csz_col + "|CSZ"
    else:
        ms = None
    out["mail_state"] = ms.where(ms.isin(US_STATE_ABBR)) if ms is not None else None
    # --- enrichment attributes (locked set 2026-06): coverage-aware pick + per-type clean.
    # Purely ADDITIVE -- the core columns above are untouched; fmap records each pick (auditable).
    def _enrich_date(s):
        if pd.api.types.is_numeric_dtype(s):
            dt = pd.to_datetime(pd.to_numeric(s, errors="coerce"), unit="ms", errors="coerce", utc=True)
        else:
            dt = pd.to_datetime(s, errors="coerce", utc=True)
            # 8-digit MMDDYYYY strings (MD Washington SS1TransDate '10162013') are not inferred
            # by pandas; parse them explicitly where the generic parse produced nothing.
            txt = s.astype("string").str.strip()
            mmddyyyy = dt.isna() & txt.str.fullmatch(r"\d{8}").fillna(False)
            if mmddyyyy.any():
                dt = dt.where(~mmddyyyy, pd.to_datetime(txt.where(mmddyyyy), format="%m%d%Y",
                                                        errors="coerce", utc=True))
        lo, hi = pd.Timestamp("1900-01-01", tz="UTC"), pd.Timestamp("2027-12-31", tz="UTC")
        return dt.where((dt >= lo) & (dt <= hi)).dt.strftime("%Y-%m-%d").reindex(gdf.index)
    _empty_int = lambda: pd.Series(pd.NA, index=gdf.index, dtype="Int64")
    for col, pats in (("mail_city", MAIL_CITY_PATTERNS), ("mail_zip", MAIL_ZIP_PATTERNS),
                      ("zoning", ZONING_PATTERNS), ("land_use_desc", USE_DESC_PATTERNS)):
        src = lov.get(col) or _pick(cols, pats, gdf=gdf)
        fmap[col] = src
        if src and src.endswith("|ZIP"):  # trailing zip of a combined postal string (WI PSTLADRESS)
            out[col] = _clean_str(gdf[src[:-len("|ZIP")]]).str.upper().str.extract(
                r"\b(\d{5})(?:-?\d{4})?\s*$", expand=False)
        else:
            out[col] = _clean_str(gdf[src]).replace("", None) if src else None
    if lov.get("mail_block") and not fmap["mailing_address"]:
        b_street, b_city, b_st, b_zip = _address_block(gdf, lov["mail_block"])
        spec = ",".join(lov["mail_block"]) + "|BLOCK"
        out["mailing_address"] = b_street.astype(object).where(b_street.notna(), None)
        out["mail_city"] = b_city.astype(object).where(b_city.notna(), None)
        out["mail_zip"] = b_zip.astype(object).where(b_zip.notna(), None)
        out["mail_state"] = b_st.where(b_st.isin(US_STATE_ABBR)).astype(object)
        out["mail_state"] = out["mail_state"].where(out["mail_state"].notna(), None)
        for c in ("mailing_address", "mail_city", "mail_state", "mail_zip"):
            fmap[c] = spec
    # zip3-derived mail_state (validated map, 98% observed-agreement gate): fills ONLY rows
    # the state/CSZ sources left NULL, so the attribute survives re-pulls by construction
    # (recon/mail_state_fill.py backfills already-normalized files the same way)
    try:
        need = out["mail_state"].isna() & out["mail_zip"].notna()
        if need.any():
            from zip_state import load_validated, zip3_series
            der = zip3_series(out.loc[need, "mail_zip"]).map(load_validated())
            if der.notna().any():
                ms_col = out["mail_state"].astype(object)
                ms_col.loc[der.index] = ms_col.loc[der.index].where(der.isna(), der)
                out["mail_state"] = ms_col
                fmap["mail_state"] = ((fmap.get("mail_state") or "mail_zip") + "|ZIP3")
    except Exception as _zs_e:  # never let the derive block a normalize
        print(f"    zip3 mail_state derive skipped: {_zs_e}", flush=True)
    yb = _pick(cols, YEAR_BUILT_PATTERNS, gdf=gdf)
    fmap["year_built"] = yb
    if yb:
        y = pd.to_numeric(gdf[yb], errors="coerce")
        out["year_built"] = y.where((y >= 1700) & (y <= 2030)).astype("Int64")
    else:
        out["year_built"] = _empty_int()
    sf_decoy = tuple(c for c in cols if re.search(SQFT_DECOY, c, re.IGNORECASE))
    sf = lov.get("building_sqft") or _pick(cols, SQFT_PATTERNS, exclude=sf_decoy, gdf=gdf)
    # lot-area guard: a candidate that equals the parcel's own area on most rows is the LOT size
    # (NYS SQ_FT, Marion ESTSQFT) -- reject it and try the next-ranked candidate
    if sf and fmap["acreage"]:
        acres_src = fmap["acreage"]
        acres = (pd.to_numeric(gdf[acres_src[:-len("/43560")]], errors="coerce") / 43560
                 if acres_src.endswith("/43560") else gdf[acres_src])
        rejected = set(sf_decoy)
        while sf:
            share = _lot_area_share(gdf[sf], acres)
            if share is None or share < LOT_AREA_MAX_SHARE:
                break
            print(f"    building_sqft: {sf} rejected as lot area ({share:.0%} of rows = acreage*43560)",
                  flush=True)
            rejected.add(sf)
            sf = _pick(cols, SQFT_PATTERNS, exclude=tuple(rejected), gdf=gdf)
    fmap["building_sqft"] = sf
    if sf:
        nsf = pd.to_numeric(gdf[sf], errors="coerce")
        out["building_sqft"] = nsf.where(nsf > 0).round().astype("Int64")
    else:
        out["building_sqft"] = _empty_int()
    for col, pats in (("land_value", LANDVAL_PATTERNS), ("improvement_value", IMPVAL_PATTERNS),
                      ("sale_price", SALE_PRICE_PATTERNS)):
        src = lov.get(col) or _pick(cols, pats, gdf=gdf)
        if src and _plausible_dollars(gdf[src]):
            fmap[col] = src
            v = _to_num(gdf[src])
            floor = NOMINAL_SALE_MAX if col == "sale_price" else 0
            out[col] = v.where(v > floor).round().astype("Int64")
        else:
            fmap[col] = None
            out[col] = _empty_int()
    sd = lov.get("sale_date") or _pick(cols, SALE_DATE_PATTERNS, gdf=gdf)
    fmap["sale_date"] = sd
    out["sale_date"] = _enrich_date(gdf[sd]) if sd else None
    if vkind == "sum":  # county publishes only land + improvement components; total = sum
        out["assessed_value"] = pd.concat(
            [_to_num(gdf[c]) for c in vsrc], axis=1).sum(axis=1, min_count=1)
    else:
        out["assessed_value"] = _to_num(gdf[vsrc]) if vsrc else None
    # canonical physical type: whole-dollar nullable int64. Rounds stray cents and coerces the
    # mixed float/int32/int64/null the various sources produce into ONE type, so a multi-file
    # read_parquet('*.parquet') never hits a ConversionException on the value column.
    out["assessed_value"] = pd.to_numeric(out["assessed_value"], errors="coerce").round().astype("Int64")
    if fmap["acreage"] and fmap["acreage"].endswith("/43560"):
        out["acreage"] = (pd.to_numeric(gdf[fmap["acreage"][:-len("/43560")]], errors="coerce") / 43560).round(4)
    elif fmap["acreage"]:
        out["acreage"] = pd.to_numeric(gdf[fmap["acreage"]], errors="coerce")
    else:
        out["acreage"] = None
    # mixed-unit acreage repair (Hamilton TN CAMA defect, likely not unique: most rows
    # hold acres but some hold SQUARE FEET in the same column — e.g. exactly 21780 for a
    # half-acre lot). The parcel polygon is the arbiter: a row claiming >100 ac whose
    # claim is ~43,560x its own polygon area is sqft. Sqft-stored slivers under 100
    # "acres" are undetectable this way but immaterial.
    if out["acreage"] is not None and getattr(out["acreage"], "notna", None) is not None:
        sus = out["acreage"].notna() & out.geometry.notna() & (out["acreage"] > 100)
        if sus.any():
            geom_ac = out.loc[sus].geometry.to_crs(5070).area / 4046.86
            ratio = out.loc[sus, "acreage"] / geom_ac.clip(lower=1e-4)
            fix = ratio[ratio > 10_000].index
            if len(fix):
                out.loc[fix, "acreage"] = (out.loc[fix, "acreage"] / 43_560).round(4)
                print(f"    acreage units: {len(fix)} sqft-stored rows converted to acres "
                      f"(polygon-area arbitrated)", flush=True)
            # claims that match NO unit signature (Hamilton: '17,962 ac' on a 38-ac
            # polygon — not acres, not sqft) are CAMA entry errors: null, don't guess
            junk_ac = ratio[(ratio > 10) & (ratio <= 10_000)].index
            if len(junk_ac):
                out.loc[junk_ac, "acreage"] = None
                print(f"    acreage units: {len(junk_ac)} no-unit-signature claims nulled "
                      f"(>10x own polygon area)", flush=True)
    out["source_url"] = entry["source_url"]
    sv = entry.get("source_last_edit")
    # layer publish date if the source has one; else derive per-row from a tax-year/date column
    out["source_vintage"] = sv if sv else _derive_vintage(gdf, cols, entry["state"])
    out["retrieved_at"] = entry["retrieved_at"]
    out = apply_account_fields(out, gdf, entry["fips"], fmap)
    # drop compact displaced junk (geometry far outside the state's window): rows with no
    # owner AND no apn are pure junk; otherwise keep the row but null the geometry
    win = _state_window(entry["fips"][:2])
    if win is not None and len(out):
        b = out.geometry.bounds
        outside = out.geometry.notna() & (
            (b.maxx < win[0] - 0.5) | (b.minx > win[2] + 0.5) |
            (b.maxy < win[1] - 0.5) | (b.miny > win[3] + 0.5) |
            ((b.maxx - b.minx) > 5) | ((b.maxy - b.miny) > 5))  # world-rings/stray vertices
        if outside.any():
            junk = outside & out["owner"].isna() & out["apn"].isna()
            keep_but_null = outside & ~junk
            if keep_but_null.any():
                out.loc[keep_but_null, out.geometry.name] = None
            out = out[~junk]
            print(f"    displaced geometries: dropped {int(junk.sum())} junk, "
                  f"nulled {int(keep_but_null.sum())}", flush=True)
    out = out[SCHEMA]

    # Re-apply the durable value overlay (backfilled assessed_value from a SEPARATE source than this
    # geometry -- OH/TN/OR assessor rolls). Registered overlays are release-critical and fail closed.
    import value_overlay
    out = value_overlay.apply_to(out, entry["fips"])

    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, os.path.basename(raw_path))
    atomic_replace_writer(out_path, lambda temp: out.to_parquet(temp, compression="zstd"))

    pop = lambda c: f"{out[c].notna().mean():.0%}"
    print(f"  {entry['state']} {entry['county']} {entry['fips']}: {len(out)} parcels | "
          f"owner {pop('owner')} apn {pop('apn')} situs {pop('situs_address')} "
          f"value {pop('assessed_value')} | map={ {k: v for k, v in fmap.items() if v} }", flush=True)
    return os.path.relpath(out_path, ROOT).replace("\\", "/"), fmap, \
        {c: round(float(out[c].notna().mean()), 3) for c in
         ("owner", "apn", "situs_address", "mailing_address", "mail_state", "assessed_value",
          "mail_city", "mail_zip", "land_value", "improvement_value", "land_use_desc",
          "zoning", "year_built", "building_sqft", "sale_price", "sale_date")}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--state")
    ap.add_argument("--fips", nargs="*")
    args = ap.parse_args()

    if not os.path.exists(MANIFEST):
        sys.exit("no manifest — run src/ingest.py first")
    import ingest as _ingest                      # retry-robust read (parallel-lane safe)
    manifest = _ingest.load_manifest(MANIFEST)

    # inject the manifest key: bulk-builder entries don't carry an inner "fips" field
    entries = [dict(e, fips=fips) for fips, e in manifest.items()]
    if args.state:
        entries = [e for e in entries if e["state"].upper() == args.state.upper()]
    if args.fips:
        entries = [e for e in entries if e["fips"] in set(args.fips)]

    out_dir = os.path.join(PARCELS, "normalized")
    print(f"Normalizing {len(entries)} counties -> {os.path.relpath(out_dir, ROOT)}")
    failed = []
    for e in sorted(entries, key=lambda x: x["fips"]):
        try:
            norm_file, fmap, populated = normalize_county(e, out_dir)
            import pyarrow.parquet as pq
            normalized_rows = pq.read_metadata(os.path.join(ROOT, norm_file)).num_rows
            source_rows = int(e.get("fetched_count") or e.get("feature_count") or normalized_rows)
            import ingest as _ingest  # lockfile-guarded manifest write (concurrent-run safe)
            _ingest.update_manifest(e["fips"], {"normalized_file": norm_file, "field_map": fmap,
                                                "populated": populated,
                                                "normalized_row_count": normalized_rows,
                                                "normalization_dropped_count":
                                                    max(0, source_rows - normalized_rows)}, MANIFEST)
        except Exception as ex:
            failed.append(e["fips"])
            print(f"  FAIL {e['fips']} {e['county']}: {ex}", flush=True)
    if failed:
        sys.exit(f"{len(failed)} counties failed: {' '.join(failed[:20])}")


if __name__ == "__main__":
    main()
