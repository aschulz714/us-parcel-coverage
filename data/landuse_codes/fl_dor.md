# fl_dor.csv — Florida DOR land use codes (DOR_UC)

**Official source:** Florida Department of Revenue, *2025 User's Guide — Department Property Tax
Data Files* (NAL/SDF/NAP), Field 8 `DOR_UC` "DOR Land Use Code", pp. 5–7.
https://floridarevenue.com/property/dataportal/Documents/PTO%20Data%20Portal/User%20Guides/2025%20Users%20guide%20and%20quick%20reference/2025_NAL_SDF_NAP_Users_Guide.pdf
(accessed 2026-06-12). Labels are verbatim from the guide.

**Code format:** fixed-length, zero-padded THREE digits, `'000'`–`'099'` (verified live against the
Florida_Statewide_Cadastral layer this dataset ingests: values arrive as strings `'001'`, `'055'`,
`'094'`...). All 100 codes are defined; the table is complete. A parser for FL must extract three
digits, not two (`landuse_std._extract_code`'s WA regex `^\s*(\d{2})` would truncate `'001'`→`'00'`).
Do **not** confuse `DOR_UC` with `PA_UC` (Field 9) — `PA_UC` is the *county-defined* appraiser code
(two digits, no statewide meaning).

**Category mapping** uses the same 8-bucket vocabulary as `wa_dor.csv` (residential, industrial,
transport/utilities, commercial-trade, commercial-services, recreation/cultural, agriculture/forest,
undeveloped/open), assigned by closest WA DOR analog. Judgment calls, documented:

- `003` (multi-family 10+) sits under the guide's "Commercial" *stratum* heading but is housing —
  mapped residential, same as WA 12/13.
- `039` hotels/motels → residential, following WA DOR's own bucketing (WA 16 hotels/motels is in
  the residential decade).
- Vacant-X codes (`000`/`010`/`040`/`070`/`080`) take their zoned category (residential/commercial/
  industrial/institutional/governmental), consistent with how OR class-x-0-0 "land only" codes map.
- `028` lumps commercial parking lots with mobile home parks; mapped transport/utilities (per the
  code's primary "parking" use, WA 46) — known imperfection: FL mobile-home-park communities coded
  028 will surface as transport/utilities. The label preserves the ambiguity.
- Government/institutional codes (`070`, `072`–`076`, `080`–`090`) → commercial-services, the WA
  precedent (WA 67 governmental / 68 educational / 65 professional services); `071` churches and
  `077` clubs → recreation/cultural (WA 72 public assembly); `082` public forests/parks and `097`
  classified-use parkland → recreation/cultural (WA 76 parks).
- `092`/`093` mining and subsurface → agriculture/forest (WA 85 mining / 89 resource production).
- `096` leads with sewage/solid-waste but FL appraisers predominantly use it for wasteland, marsh,
  dunes, swamps → undeveloped/open (WA 91/93).
- `098` centrally assessed = railroads and private-car lines in FL → transport/utilities.
