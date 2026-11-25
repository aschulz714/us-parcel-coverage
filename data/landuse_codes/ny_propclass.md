# NY land-use code table — `ny_propclass.csv`

**Source standard:** New York State **Property Type Classification System** (NYS ORPTS) — the
3-digit statewide property class codes carried in the NYS Tax Parcels Public layer's `PROP_CLASS`
field (mapped to our `land_use`). Applied via `STATE_TABLES["NY"] = ("ny_propclass.csv", "3digit")`.

**Coverage achieved:** non-NYC NY counties **0% → 99.4%** `land_use_category` (77.2% statewide; the
gap is NYC, below). 319 distinct 3-digit codes present in the data, all mapped.

## Scope decision — NYC excluded (intentional)
The 5 boroughs (Bronx 36005, Kings 36047, New York 36061, Queens 36081, Richmond 36085 — ~854k
parcels / 22.4% of NY) publish **2-digit NYC PLUTO land-use codes (01–11)**, a different system.
These are **left unmapped** (out of scope per Alex, 2026-06-21). The `3digit` parse rule zero-pads
them to `001`–`011`, which are absent from this table, so they resolve to null automatically — no
risk of a wrong NYS-code collision (NYS codes start at 100).

## Mapping conventions (follow the existing tables, esp. `fl_dor.csv`)
Categories: `agriculture/forest`, `residential`, `undeveloped/open`, `commercial-trade`,
`commercial-services`, `industrial`, `recreation/cultural`, `transport/utilities`.

| NYS range | Meaning | → category | Notes |
|---|---|---|---|
| 100–199 | Agricultural | agriculture/forest | all |
| 200–299 | Residential | residential | 1/2/3-family, mobile homes, seasonal, estates |
| **300–399** | **Vacant land** | **undeveloped/open** | **Decision:** the whole "Vacant Land" class → undeveloped/open, keeping vacant distinct from improved (more useful for real-estate / tax-appeal comps than splitting vacant by intended use) |
| 400–499 | Commercial | commercial-trade (default) | exceptions below |
| 410–418 | Apartments / hotels / motels / MH parks | residential | use-based (matches FL: multi-family & hotels → residential) |
| 440–449 | Storage / warehouse / trucking / lumber | industrial | matches FL warehousing convention |
| 460–465 | Banks / office buildings | commercial-services | |
| 470–472 | Personal/professional services | commercial-services | |
| 407,435,436,438,439 | Parking | transport/utilities | |
| 500–599 | Recreation & entertainment | recreation/cultural | all |
| 600–699 | Community services | commercial-services (default) | 620 religious, 680–682 cultural/rec → recreation/cultural; 692 ROW → transport/utilities |
| 700–799 | Industrial | industrial | incl. mining/wells (NYS classes them 700) |
| 800–899 | Public services / utilities | transport/utilities | comms, transport, electric/gas/water, waste |
| 900–999 | Wild / forest / conservation / parks | agriculture/forest (default) | 920/960–970 parks & clubs → recreation/cultural; 950/971/972/990s conservation/water/misc → undeveloped/open |

## Realized category mix (measured, non-NYC ≈ 2.97M parcels)
residential 77.1% · undeveloped/open 14.1% · agriculture/forest 2.5% · commercial-trade 2.3% ·
commercial-services 1.2% · transport/utilities 1.1% · recreation/cultural 1.0% · industrial 0.8%.

**Caveat:** the 8-category target is coarse vs. NYS's ~250 codes; a few use-based calls are judgment
(apartments→residential, mining→industrial, religious→recreation/cultural). The raw NYS code is
preserved in `land_use` and the matched code in `land_use_code` for anyone needing finer detail.
