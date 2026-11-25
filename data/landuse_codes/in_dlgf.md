# in_dlgf_proposed.csv — Indiana DLGF property class codes (proposed for data/landuse_codes/in_dlgf.csv)

**Official source:** Indiana Department of Local Government Finance, *Property Tax Management
System Code List Manual* (revised May 27, 2026), **Code List 1 — Property Class Codes** (pp. 3–7),
applied to the PARCEL-table "Property Class Code" field required for county data submission to
DLGF/LSA under 50 IAC 26-20-3.
https://www.in.gov/dlgf/files/2026-memos/Property-Tax-Management-System-Code-List-Manual-260514.pdf
(accessed 2026-06-12). Labels are verbatim from the manual (180 codes; "CAFÉ" transcribed as
"CAFE", ASCII-safe).

**What the dataset carries:** the IN preflight (recon/in_preflight.md) found this 3-digit code on
~20 of the 34 arcgis county layers (`prop_class`, `PROPCLASS`, `PCC_Code`, `CLASSCODE`,
`PropertyClassNum`, `prop_class_code`...). Observed values (100, 425, 501, 510, 511, 556, 599,
620, 640, 680, 141, 149...) all resolve against this table. Code structure: 1xx agricultural,
200 mineral, 3xx industrial, 4xx commercial, 5xx residential, 6xx exempt (by owner type),
8xx utilities (locally/state assessed). **No 7xx or 9xx codes exist in the official list** —
county-local junk values like `999` (seen in Wells) should stay unmatched (honest null), exactly
like unmatched WA codes. Exact 3-digit match; no prefix logic needed.

**Category mapping** (8-bucket vocabulary shared with wa_dor/fl_dor/tx_ptad/or_propclass),
judgment calls documented:
- `4xx` splits trade vs services by code (retail/food/auto → commercial-trade; banks/offices/
  medical/funeral/laundry → commercial-services), mirroring the WA 50s/60s decades.
- Apartments (`401–403`), hotels/motels (`410/411` — WA 16 precedent), nursing homes (`412`),
  mobile home parks (`415`), other housing (`419`) → residential; campgrounds (`416`) →
  recreation/cultural (WA 75).
- Warehouses (`350`, `480/481`) → industrial (FL 048 precedent); truck terminals (`360`, `482`)
  and marinas/marine service (`490–496`) → transport/utilities (WA 42/44); parking (`456`) →
  transport/utilities (WA 46).
- `380` mines/quarries → agriculture/forest (the cross-state resource-extraction convention:
  WA 85, FL 092, TX G*) even though Indiana classes it industrial; `385` landfill →
  transport/utilities (waste disposal).
- `6xx` exempt maps by owner type per the WA-67 governmental precedent (→ commercial-services),
  with use-specific exceptions: `645` housing authority → residential, `660` park district →
  recreation/cultural (WA 76), `661/662` conservancy/sanitary districts → transport/utilities,
  `685/686` religious → recreation/cultural (WA 72).
- All `8xx` (bus/power/pipeline/railroad/sewage/telecom/water companies) → transport/utilities.

**To adopt:** review, then move to `data/landuse_codes/in_dlgf.csv` + rename this file to
`in_dlgf.md` alongside it (same layout as fl_dor.csv/fl_dor.md). The IN normalizer parse is an
exact 3-digit match against the raw `land_use` values captured per county in the preflight.
