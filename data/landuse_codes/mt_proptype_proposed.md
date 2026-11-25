# mt_proptype_proposed.csv — Montana cadastral `PropType` (PARTIAL mapping — honest limit)

**Official source:** Montana State Library, MSDI Cadastral framework — the Parcels layer
this dataset ingested (gisservicemt.gov MSDI_Framework/Parcels, sourced from MT DOR's
Orion CAMA; https://msl.mt.gov/geoinfo/msdi/cadastral/, accessed 2026-06-12). The 22-value
PropType domain was captured live from the layer (full enumeration = this table).

**THE HONEST FINDING: MT PropType is an appraisal *property type*, not a land-use code.**
Its dominant value `Improved Property` (429,095 parcels = 48.6% of typed rows) covers any
improved real property — residential AND commercial — and cannot be assigned to one of
our 8 categories. Likewise `Tribal Property` (18,599), `Non-Valued Property` +
`Non-Valued with Specials` (5,284), `Partial Exempt Property` (711), `On Leased Land`
(708), `Improved - Deed Restricted` (66). These rows keep a table entry with a BLANK
category (honest null).

**Phase-2 measured (final parquets, 2026-06-12): 429,240/920,558 = 46.6% of all parcels
(48.6% of non-null — exactly the projection).** Worst counties 26–30% (rural counties
dominated by `Improved Property`).

**What IS mappable — 429,244 of 883,707 typed rows = 48.6%:** Vacant Land 286,719 →
undeveloped/open; Exempt Property 101,429 → commercial-services; Condominium + Townhouse +
Apartment + Mobile/RV Parks 32,210 → residential; Mining Claim + Gravel Pit + Industrial +
IR + Oilfield 6,521 → industrial; CA/CN Centrally Assessed (MT DOR centrally assesses
utilities/railroads/pipelines) 2,170 → transport/utilities; Golf Course 193 →
recreation/cultural.

**Parser rule:** exact full-string match after trim (values arrive as the expanded labels
shown; three rare values carry `XX - ` code prefixes — match the string exactly as
served).

**Future refinement (lead's call, beyond this table) — now MEASURED (2026-06-12, see
recon/landuse_interim_findings.md):** within the 429,095 `Improved Property` rows,
**44,462 (10.4%) carry ag acreage > 0** in the layer's DOR breakdown fields (GrazingAcres
35,800 · FarmsiteAcres 25,665 · FallowAcres 13,828 · ForestAcres 10,796 · IrrigatedAcres
9,862 · WildHayAcres 6,721 · ContinuousCropAcres 557). An "Improved + any ag acreage →
agriculture/forest" rule lifts MT coverage 48.6% → 53.6%. Caveat: these fields are not in
the normalized parquets — needs a slice-fetch or re-ingest inclusion.
