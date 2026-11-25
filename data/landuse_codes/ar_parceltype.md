# ar_parceltype_proposed.csv — Arkansas `parceltype` (CAMP parcel polygons)

**Official source:** Arkansas GIS Office, *Parcel Polygon — County Assessor Mapping
Program*, FGDC metadata
(http://gis.arkansas.gov/Metadata/HTML/asdi.Planning_Cadastre.PARCEL_POLYGON_CAMP_export.html,
accessed 2026-06-12): ParcelType = "Property Type Code. A code indicating the property's
use or classification at the time of reappraisal…  A list of approved property type codes
is available from the Arkansas Assessment Coordination Department." **The ACD code list is
not published online** (searched 2026-06-12); the GeoStor REST layer carries no field
domain either (verified: `domain: null`).

**Legend provenance — EMPIRICAL (2026-06-12), not official:** structure is 1st char = use
class (R/A/C/I/E + PS/V/M), 2nd char = subtype (I improved, V vacant, M mobile-home, …).
Verified by sampling owner names per code on the source layer:
- `EG` → "USA", "STATE OF ARKANSAS" (government) · `ER` → churches · `ES` → school
  districts · `EC` → Masonic lodge / hospital / community facility (charitable) ·
  `EB` → cemeteries · `EF` → volunteer fire associations · `EL` → libraries.
- `PS` → telephone companies (public service) → transport/utilities.
- `I9` → "CARROLL COUNTY ACT 9 BOND" ×3 (Act 9 industrial revenue bond facilities).
- `IA` → Green Bay Packaging / Suzano / Pinecrest Lumber (agricultural-processing mills).
- `VP` → "(REFERENCE)" plat records — non-parcel rows, left uncategorized.
- `IF` → property-owner associations / subdivision common areas — left uncategorized
  (NOT industrial despite the I prefix; the empirical check overruled the structure).
- `RB`/`RC`/`RF` → mixed municipal/individual owners, inconsistent with a residential
  reading — left uncategorized (2026-06-12 samples: CITY OF GRAVETTE, TOWN OF DEQUEEN).

**Vacant codes (`RV`/`AV`/`CV`/`IV`) take their zoned use's category** — the fl_dor.md
convention for vacant-zoned codes.

**Parser rule:** exact 2-character match after trim + uppercase (`exact` rule; no
extraction).

**Projected coverage (source-layer groupBy of all 41 codes, 2,121,540 parcels,
2026-06-12):** categorized codes cover 2,119,767 parcels = **99.9%**; the 8 uncategorized
codes (VP, RB, RC, RF, IF, ML, BB, AC) total 1,773 parcels (0.08%).

**Phase-2 measured (final parquets, 2026-06-12): 2,116,007/2,117,780 = 99.9%** (worst
county 99%). Amendment found by the Phase-2 run: `EI` (3,855 parcels) was missing from
the original table — owner-sampled (AR State Highway Commission / University of Arkansas /
USFS = state-institutional exempt) and added → commercial-services. Without it AR
measured 99.7%.
