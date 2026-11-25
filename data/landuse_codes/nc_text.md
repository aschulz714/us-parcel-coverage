# nc_text_proposed.csv — North Carolina free-text `parusedesc` keyword table

**Source:** NC OneMap statewide parcels — the layer this dataset ingested
(services.nconemap.gov NC1Map_Parcels/MapServer/1). NC has **no statewide land-use code
standard**: the NC CGIA statewide parcels schema defines `parusecode`/`parusedesc` as the
*county's own* parcel use code/description. Verified live 2026-06-12: 4,081 distinct raw
`parusedesc` values across 5,938,640 parcels (full distribution saved in
recon/probe_results/landuse_domains.json; the groupBy total ties out exactly to our
ingested NC parcel count).

**PIPELINE PREREQUISITE (blocking):** normalize.py mapped **no land_use column for any of
the 100 NC counties** (manifest field_map `land_use=None` statewide) — NC parquets carry
no raw land-use values today. Activation requires adding the field to the NC mapping
(re-normalize), then applying this table. **Measured field choice (2026-06-12, see
recon/landuse_interim_findings.md): `land_use = coalesce(parusedesc, parusedsc2)`** —
parusedesc is the best-populated candidate (4,295,002 = 72.3% vs parusecode 3,082,651),
and parusedsc2 recovers 189,043 of the parusedesc-blank parcels (non-blank 72.3% → 75.5%).

**Parser rule: NEW `text` mode (spec in recon/landuse_activation.md).** EXACT match of the
normalized string — uppercase, collapse internal whitespace to single spaces, trim. NO
substring matching (substrings not proven safe: "RES" sits inside "PRESBYTERIAN", "AG"
inside "GARAGE", "PARK" inside "PARKER"). The `code` column of this CSV holds the
normalized text key; `label` records the statewide parcel count behind each key.

**Coverage (measured on the source layer, 2026-06-12):** blank/null = 1,643,638 parcels
(27.7% — missing at source, unfixable). The 300 keys in this table cover **3,935,585
parcels = 66.3% of all NC parcels = 91.6% of non-blank**. Extending to the top-1,000 keys
would reach 99.3% of non-blank (+~330k parcels) — diminishing returns; revisit after the
Phase-2 parquet measurement.

**Method:** keyword rules + 22 hand overrides, all 300 assignments reviewed
(recon/gen_landuse_tables.py; review log recon/probe_results/nc_review2.txt). Notable
calls: `SFR GOLF`/golf-frontage homes → residential (NOT recreation — the golf COURSE
itself maps recreation/cultural); `OFFICE CONDOMINIUM` → commercial-services and
`WAREHOUSE CONDOMINIUM` → industrial (not residential despite "condominium"); bare `V` →
undeveloped/open (corroborated by `V-VACANT`); vacant-zoned strings ("VACANT COMMERCIAL",
"91.00-VACANT SF RES") → undeveloped/open (NC counties write vacancy as the primary fact,
unlike FL's zoned-vacant codes — documented divergence from the fl_dor vacant convention);
`HOUSE ON COMMERCIAL SITE` → commercial-trade (zoned-use convention). Honestly
uncategorized: `IMPROVED`, `OCCUPIED RURAL`, `RURAL IMPROVED`, `AUXILIARY IMPROVEMENT*`,
single letters (`I`,`E`,`C`,`RA`,`D`,`F`,`G`,`A`,`B`,`O`,`P`,`Y`), bare numerics
(`511`,`500`,`501`,`510`,`561`), `F100/F101/F106/F108-RURAL`, `UNK-LEASEHOLD/CONDO`,
`NEW PARCEL`, `WATERFRONT IMPROVED` — ambiguous without county context.
