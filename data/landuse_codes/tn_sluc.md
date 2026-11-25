# tn_sluc.csv — Tennessee Standard Land Use Codes (SLUCM Table 1.0)

**Source:** Hamilton County TN "AssessorExtractLayout.pdf" pages 12–19 ("STANDARD LAND
USE CODES TABLE 1.0"), cached at `data/parcels/bulk_src/hamilton_tn/`. The table is
Tennessee's adoption of the federal Standard Land Use Coding Manual (SLUCM, 1965)
hierarchy — top-level bands: 100 Residential, 200/300 Manufacturing, 400
Transportation/Communications/Utilities, 500 Wholesale & Retail Trade, 600 Services,
700 Cultural/Entertainment/Recreation, 800 Resource Production & Extraction, 900
Undeveloped Land & Water.

**Category mapping:** band → our 8-category vocabulary by leading digit
(1→residential, 2/3→industrial, 4→transport/utilities, 5→commercial-trade,
6→commercial-services, 7→recreation/cultural, 8→agriculture/forest,
9→undeveloped/open). One override: 850 Mining → industrial (extraction is not
agriculture). Codes seen in county data but absent from the PDF's leaf list
(e.g., 118, 124, 144) get a generic band label and the band category — the leading
digit is authoritative for category in a hierarchical system even when the leaf
label is unknown.

**Coverage reality (2026-06-12):** numeric SLUC codes appear in Hamilton County
(~168k parcels). Most other TN counties publish free-text (`RESIDENTIAL`,
`COMMERCIAL`) or nothing in the statewide layer; those parse to null under the
3digit rule, which is honest. If more TN counties with numeric SLUC join later,
this table covers them with zero extra work.
