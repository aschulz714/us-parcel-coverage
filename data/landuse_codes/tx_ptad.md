# tx_ptad.csv — Texas property classification codes (stat_land_use)

**Official source:** Texas Comptroller of Public Accounts, *Property Classification Guide —
Reports of Property Value*, publication 96-313: Exhibit 1 "Property Classifications" (category
codes + titles, verbatim), Category J subcodes J1–J9 (p. 22), Exhibit 7 "Exemption
Classifications" XA–XV (pp. 31–32). https://comptroller.texas.gov/taxes/property-tax/docs/96-313.pdf
(accessed 2026-06-12).

**What the dataset carries:** the TxGIO StratMap land-parcels `stat_land_use` field holds these
PTAD codes as reported by each county appraisal district. Verified against raw data (Bowie 48037):
`A1, D1, E1, C1R, C1S, F1, D2, A2, A2L, C1C, C1A, X, XC1, E2, O1, B2, XF1, XD1, F2, XD2` —
CADs freely append **local suffixes** to the official codes (`A1`/`A2` under `A`; `C1R`/`C1S`
under `C1`; `XC1`/`XD1` = exempt + the underlying class). A parser must therefore use
**longest-valid-prefix matching** against this table, not exact match: `C1R`→`C1`, `A2L`→`A`,
`XC1`→`X`, `H1`→`H`. Every code in the Bowie sample resolves this way. (The WA two-digit regex in
`landuse_std._extract_code` does not apply to TX.)

**Completeness caveats (verified absences, not omissions):**
- Exhibit 7 itself jumps `XA`→`XD` (no `XB`/`XC` defined) and says it "does not include all
  exemptions"; unlisted exempt variants prefix-match to `X`.
- Category `N` (intangible personal property) existed in older editions of the guide but is **not**
  in the current 96-313; legacy rolls carrying `N` will simply not match (honest no-category).
- The guide's prose uses `H1` for personal vehicles but Exhibit 1 defines only `H`; `H1`/`H2`
  resolve to `H` by prefix.

**Category mapping** (8-bucket vocabulary shared with `wa_dor.csv`/`fl_dor.csv`):
- `E` (rural land not ag-qualified + improvements) → residential: PTAD E is predominantly rural
  homesteads/small acreage; ag-qualified land is D1/D2.
- `F1` generic "Commercial" → commercial-trade (TX, like OR class 2, doesn't split trade vs
  services; one bucket chosen, documented — same choice as OR 2xx).
- `G1`–`G3` oil/gas/minerals → agriculture/forest (WA 85 mining / 89 resource production analog).
- `O` residential inventory (platted builder lots) → residential.
- `X*` exempt → commercial-services per the WA-67 governmental-services precedent, except where the
  exemption names a concrete use (XI youth orgs → recreation/cultural, XR water/wastewater →
  transport/utilities, XP offshore rigs → industrial, XQ dredge site → undeveloped/open).
- **Blank category = personal-property classification** (`H`, `L1`, `L2`, `M2`, `S`, `XM`, `XN`,
  `XO`, `XS`): these describe chattels, not land use; land_use_code still records them, the
  category column is honestly empty (maps to null land_use_category downstream).
