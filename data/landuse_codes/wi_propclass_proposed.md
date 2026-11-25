# wi_propclass_proposed.csv — Wisconsin DOR assessment classes (PROPCLASS + AUXCLASS)

**Official source:** *V10 Wisconsin Statewide Parcels Schema Documentation* (WI Dept of
Administration / State Cartographer's Office),
https://www.sco.wisc.edu/parcels/data/assets/V10/V10_Wisconsin_Statewide_Parcels_Schema_Documentation.pdf
(accessed 2026-06-12), p. 18: PROPCLASS = "The General class of property for taxable real
estate, as specified in Wisconsin s. 70.32(2)(a)"; labels verbatim (1 Residential,
2 Commercial, 3 Manufacturing, 4 Agricultural, 5 Undeveloped, 5M Agricultural forest,
6 Productive Forest Land, 7 Other). AUXCLASS labels verbatim from the same page
(X1 Federal, X2 State, X3 County, X4 Other exempt; W1–W9 Forest Cropland/MFL programs;
AW/AWO = "assessed with" markers under s. 70.23(2), not uses).

**Field used in our data:** `PROPCLASS` (manifest field_map, all 72 ingested WI counties).
`AUXCLASS` is NOT currently ingested — see caveat 3.

**Value format (verified live on the statewide layer, 2026-06-12):** single class (`'1'` =
2,132,389 parcels) or comma-delimited multi-class (`'4,5'` 119,366; `'4,5,5M'` 64,248;
`'1,6'` 45,400; …). The doc specifies "No spaces in between values" but AUXCLASS values
like `'W4, X2'` WITH a space exist live — the parser must trim tokens.

**Parser rule: NEW `multiclass` mode (spec in recon/landuse_activation.md).** Split on
`,`, trim + uppercase each token, map each via this table; resolve multi-token parcels by
the precedence order **3 > 2 > 1 > 4 > 5M > 6 > 5 > 7** (improved/value-dense use wins:
a parcel that is part residence, part forest is the residence's parcel). AW/AWO tokens
are skipped.

**Judgment calls:**
- Class 2 "Commercial" is undifferentiated trade+services → commercial-trade, following
  tx_ptad.csv's F1 convention.
- Class 7 "Other" is statutorily farm buildings/improvements + their sites (WPAM) →
  agriculture/forest (alternative reading: residential for farm dwellings; flagged).
- X1–X4 exempt → commercial-services (tx_ptad X convention); W1–W9 forest-program land →
  agriculture/forest.

**Caveats:** (1) parcels classified only in AUXCLASS will not categorize until AUXCLASS
is ingested — recommend normalize.py fall back land_use→AUXCLASS when PROPCLASS is null
for WI at the next re-normalize; (2) parquet-measured coverage = Phase 2 (parquets
mid-rewrite at time of writing); source-layer PROPCLASS distribution saved in
recon/probe_results/ (top combos).
