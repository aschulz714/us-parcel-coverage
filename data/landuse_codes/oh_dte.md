# Ohio DTE land-use codes (oh_dte.csv)

Ohio Department of Taxation 3-digit land-use / property-class code standard, applied statewide
(county auditor `usecd` / `LUC` / `Class` fields). Used by `landuse_std` with the **`3digit`** rule
(`STATE_TABLES["OH"]`), same as FL/OR/TN — numeric values are normalized to a zero-padded 3-digit
string and looked up. Only the **7 self-hosted OH metros** carry a use code (Fairfield/Gallia/
Lucas/Meigs/Montgomery/Richland/Summit); the 81 ODNR statewide-slice counties have **no** land-use
field, so they categorize 0% honestly.

## Sources (verified live 2026-06-14, multi-agent cross-check)
- **Greene County Auditor — "Auditor's Land Use Codes"** (canonical statewide DTE list, label-verbatim
  100s–720): `https://www.greenecountyohio.gov/DocumentCenter/View/23949/Land-Use-Codes`
- **Defiance County Auditor — Land Use Codes** (700s 730/740 + 800s utility/railroad band):
  `https://www.defiance-county.com/auditor/land-use-codes.php`
- **Ohio Administrative Code 5703-25-10** (statutory band definitions, effective TY1985):
  `https://codes.ohio.gov/ohio-administrative-code/rule-5703-25-10`
- Sample checks: `111`=CAUV cash-grain/general farm → agriculture/forest; `510`=single-family
  dwelling → residential; `499`=other commercial → commercial-services.

## Bands → category mapping
- **100–199 Agricultural** (incl 120/121 timber) → `agriculture/forest`
- **200–299 Mineral land/rights** → `industrial` (extraction)
- **300–399 Industrial** → `industrial`
- **400–499 Commercial** → split: retail/stores/shopping/restaurants/auto-sales → `commercial-trade`;
  offices/banks/medical/hotels/services/warehouses → `commercial-services`; theatres/golf/bowling/
  amusement/marinas/campgrounds → `recreation/cultural`; **apartments (401–403), mobile-home parks
  (415), other commercial housing (419) → `residential`** (commercial-band by tax class, but the
  USE is residential); truck terminals (482) + utility-owned (489) → `transport/utilities`
- **500–599 Residential** (third digit = tract size; size variants enumerated) → `residential`
- **600–699 Exempt** → mapped by USE where determinable: schools/parks/colleges/churches/cemeteries
  (650/660/670/685/690) → `recreation/cultural`; charitable hospitals/homes (680) → `commercial-services`;
  metro housing authority (645) → `residential`. **Pure owner-based exempt codes (600–640: US/State/
  county/township/municipal) are left UNCATEGORIZED** — the code denotes owner, not use.
- **700–799 Tax abatements** → **UNCATEGORIZED** (these are abatement flags, not a use)
- **800–899 Public utility / railroad** → `transport/utilities` (railroad/utility ops 840–881);
  utility-owned land of a stated use (800 ag→agriculture/forest, 810/820 →industrial, 830→commercial-services)

## Judgment calls / flags (review-worthy)
- **Apartments & mobile-home parks → residential** (not commercial) — categorizing by use, not tax band.
- **Mineral (200s) → industrial** — no "mineral" category exists; extraction is the closest.
- **Code 489** is ambiguous in sources (appears in Defiance's 800s context as utility commercial/
  industrial) → mapped `transport/utilities`; rare.
- **820/830** labels are from OAC summary, not the Defiance list (slightly less certain).
- Owner-based exempt (600–640) and abatement (700–740) codes intentionally blank → uncategorized.

*Built 2026-06-14 from the multi-agent OH-ingest preflight follow-up. Codes are the published DTE
standard; category assignments are this project's taxonomy mapping.*
