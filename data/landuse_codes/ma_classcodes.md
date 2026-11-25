# MA land-use code table — `ma_classcodes.csv`

**Source standard:** Massachusetts DOR **Property Type Classification Codes** (the state-class codes
in the assessor `land_use` field). Applied via `STATE_TABLES["MA"] = ("ma_classcodes.csv", "prefix")`.

**Coverage achieved:** MA `land_use_category` **0% → 99.6%** (2.55M parcels; lowest county 99%).

## Why the `prefix` rule
MA carries the 3-digit DOR base class (e.g. `101` single-family) **plus** 4-digit local extensions
(`1010` = base `101` + a local subclass digit). Roughly half the parcels use each form. The `prefix`
parse rule (longest-matching code prefix) maps a 4-digit code via its 3-digit base — so `1010`, `1013`,
`1021` all resolve through `101`/`102`. The table holds the **390 distinct 3-digit base codes present**.

## Mapping conventions (category vocab follows `fl_dor.csv`)
| MA leading digit | Class | → category | Refinements |
|---|---|---|---|
| 0xx | Mixed use | residential | MA mixed-use is residential-primary (e.g. 3-family + storefront) |
| 1xx | Residential | residential | **130–139 vacant residential land → undeveloped/open**; 101–129 (incl. condos, apartments) → residential |
| 2xx | Open space | undeveloped/open | sparse |
| 3xx | Commercial | commercial-trade | 300–305 hotels/motels → residential; 340–369 offices/banks/medical → commercial-services |
| 4xx | Industrial | industrial | |
| 6xx | Ch.61 forest | agriculture/forest | |
| 7xx | Ch.61B recreational | recreation/cultural | |
| 8xx | Ch.61A agricultural | agriculture/forest | |
| 9xx | **Exempt** | commercial-services | vacant-exempt (930/940/950/960 = vacant municipal/county/state/federal) → undeveloped/open; rest (govt buildings, charitable, religious, educational) → commercial-services |

## Realized category mix (measured, 2.55M parcels)
residential 83.1% · undeveloped/open 7.7% · commercial-services 5.3% · commercial-trade 2.2% ·
industrial 1.0% · recreation/cultural 0.3% · agriculture/forest 0.3%.

**Caveats (documented judgment):** (1) MA **9xx "exempt" is a tax-status class, not a use class** — a
municipal building and a state park can share a 9-prefix; we route vacant-exempt to undeveloped/open and
the rest to commercial-services, which is approximate. (2) `0xx` mixed-use → residential. (3) `995/996`
(~25k parcels) are exempt-other of uncertain use → commercial-services. The raw code is preserved in
`land_use` and the matched base in `land_use_code` for anyone needing finer resolution.
