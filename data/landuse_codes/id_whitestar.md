# id_whitestar_proposed.csv — Idaho `landusecod` first-digit scheme (vendor field, empirical)

**Source:** the ingested ID statewide layer is the **WhiteStar parcel aggregate**
(gis1.idl.idaho.gov Portal/WhiteStar_Parcels; manifest field_map uses `landusecod`, with
free-text sibling `landusedes`). WhiteStar is a commercial aggregator; **no public
documentation of its land-use code scheme was found** (searched 2026-06-12). Idaho itself
has no statewide assessor land-use standard — counties file Idaho STC *category* codes,
which surface only inside the descriptions (e.g. "RES IMPROVEMENTS ON CAT 20",
"IMPROVEMENTS ON CAT 12").

**Legend provenance — EMPIRICAL cross-tab (2026-06-12):** grouped `landusecod ×
landusedes` over the full source layer (top-150 pairs saved in
recon/probe_results/landuse_domains.json). First-digit semantics are consistent across
counties:

| 1st digit | rows | dominant descriptions |
|---|---|---|
| 1xxx | 716,293 | SINGLE FAMILY RESIDENTIAL / DWELLING / MOBILE HOME / COMMON AREA |
| 2xxx | 32,622 | COMMERCIAL (GENERAL) / COMMERCIAL CONDO |
| 3xxx | 2,979 | OFFICE / GENOFF / COMMERCIAL OFFICE |
| 4xxx | 448 | CAMPGROUND, RV PARK / RECREATIONAL SITES |
| 5xxx | 3,105 | STORAGE WAREHOUSE / IND IMP / LMFG |
| 6xxx | 714 | AIRPORT PROPERTY / BULK STORAGE TANKS / DISTRIBUTION WAREHOUSE |
| 7xxx | 106,147 | DRY GRAZING LAND / IRRIGATED AG LAND / AGRICULTURAL (GENERAL) |
| 8xxx | 143,093 | VACANT LAND / RURAL RESIDENTIAL TRACT (vacant lots) |
| 9xxx | 59,921 | EXEMPT PROPERTY / EXEMPT PROP STATE STATUTE |
| 0xxx | 17,937 | MISCELLANEOUS / IMPROVED USE NOT SPECIFIED / WASTE-ROW → left uncategorized |

**Parser rule: NEW `digit1` mode (spec in recon/landuse_activation.md):** trim; value must
match `^\d{4}$`; map by FIRST digit via this 10-row table (the "0" row carries no
category). Non-4-digit values — blank `' '` (49,336), 3-digit county strays ('550',
'101', …), letters ('L', 'R') — are honestly left uncategorized.

**Measured on the source layer:** 4-digit non-0xxx codes cover 1,065,322 of 1,136,910
grouped rows = **93.7%**.

**Phase-2 measured (final parquets, 2026-06-12): 1,065,461/1,137,058 = 93.7% of all
parcels (98.0% of non-null)** — matches the projection exactly. Worst counties 62–75%
(the 3-digit county-stray class, as documented).
