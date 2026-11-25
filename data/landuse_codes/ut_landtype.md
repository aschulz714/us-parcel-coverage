# UT land-use code table — `ut_landtype.csv`

**Source:** Utah statewide assessor **land-type free text** (37 distinct values in `land_use`).
Applied via `STATE_TABLES["UT"] = ("ut_landtype.csv", "text")` — exact case-insensitive text match.

**Coverage achieved:** UT `land_use_category` **0% → 80.8%** (1.85M parcels). The ~19% gap is honest:
the unmapped values carry **no use information** — `Unknown` (161k), `Tax Exempt`/`Exempt` (status, not
use), `Personal Property`, and valuation-method flags (`NON-FAA`, `FAA Mkt`, `Privilege Tax`, `DEFAULT`,
`New`) — so they are left null rather than guessed.

## Mapping (category vocab follows `fl_dor.csv`)
- **residential** — Residential, Single Family, Multi-Family, Apartments, Commercial - Apartment & Condo,
  Subsidized Housing, Mixed Use, Personal Property Mobile Home
- **undeveloped/open** — Vacant, Undeveloped, Land, Land with Minimal structures, Vacant/Vacant - Commercial
- **commercial-trade** — Commercial, Commercial - Retail
- **commercial-services** — Commercial - Office Space, Tax Exempt - Government / Other / Charitable-Religious
- **industrial** — Industrial, Commercial - Industrial
- **agriculture/forest** — Agricultural, Greenbelt (ag-assessed), Vacant - Agricultural
- **recreation/cultural** — Recreational
- **transport/utilities** — Centrally Assessed (UT state-assessed utilities/transport/mines)

**Judgment calls:** Mixed Use → residential; the three `Tax Exempt - *` use-bearing variants →
commercial-services (institutional); generic `Tax Exempt`/`Exempt` left null (status only). Raw text
preserved in `land_use`.
