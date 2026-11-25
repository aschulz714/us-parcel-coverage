# wv_ias_proposed.csv — West Virginia IAS LandUseCode (via ParcelSummary JOIN)

**Source:** WVU GIS Technical Center / WV Property Tax Division statewide parcel service —
the SAME MapServer this dataset ingested
(services.wvgis.wvu.edu/.../Planning_Cadastre/WV_Parcels/MapServer). Geometry layer 0
carries **no use field** (verified 2026-06-12: 17 fields — owner/legal/address only), but
**table 11 `ParcelSummary` carries `LandUseCode` + `LandUse`** (plus PropertyClassCode
R/F/X/C/U/A/I and TaxClass 1–4), joinable on `CleanParcelID`, which exists on both layer 0
and table 11. Labels in this table are VERBATIM from the data (embedded as
"101 - Residential 1 Family" strings); the scheme is the WV Integrated Assessment System
(IAS) land-use vocabulary shared by all 55 counties' CAMA (structured: 1xx residential,
2xx apartment, 3xx commercial, 4xx industrial/mining, 6xx exempt/civic, 7xx utility).
Full 224-code distribution saved in recon/probe_results/landuse_domains.json.

**PIPELINE PREREQUISITE (blocking):** WV parquets carry no land_use (manifest field_map
`land_use=None` for all 55 counties). Activation requires an ingest-side attribute join —
the existing arcgis-join machinery (`_fetch_attr_table` + local merge on CleanParcelID) or
a landuse_std slice-fetcher like WA's `_statewide_slice`, pulling
`CleanParcelID,LandUseCode` from table 11. The parcels↔ParcelSummary join match-rate is
unmeasured (Phase 2 / first join test); ParcelSummary holds 1,499,827 rows vs ~1.39M
ingested WV parcels.

**Mapping method:** leading-digit range defaults (1xx/2xx → residential, 3xx →
commercial-trade, 4xx → industrial, 6xx → commercial-services, 7xx → transport/utilities)
plus ~40 reviewed overrides following house conventions: hotels/nursing/boarding (314/315/
316/318) → residential (WA 16/17 convention); parking (338/339) + truck terminal (395) →
transport/utilities (FL 028); warehousing (391–399) → industrial; offices/banks/medical/
funeral/vet/daycare (349–355, 361/362/369) → commercial-services; clubs/golf/recreation/
theaters/bowling (363–367, 381–390) → recreation/cultural (FL 077/038); cemetery (601) →
commercial-services (FL 076); vacant-zoned codes (100/200/300/400) → their zoned category
(fl_dor convention); 600 vacant-exempt + 114 conservation easement + 123 unknown-potential
tract → undeveloped/open; 449 pipeline + 464 steam plant + 690 rail/bus/air terminal →
transport/utilities; 112/113 farms → agriculture/forest. (PropertyClassCode F "Farm" exists
as a 7-value fallback if the lead prefers coarse classes — not needed given LandUseCode.)

**Coverage (measured on ParcelSummary, 2026-06-12):** 223 codes; categorized rows =
1,496,162 / 1,499,827 = **99.8%**. 41 codes have blank labels at source (3,665 rows total)
and are mapped by their numeric range with a flag in the label — the lead may prefer to
null those; the list is printed by recon/gen_landuse_tables.py.
