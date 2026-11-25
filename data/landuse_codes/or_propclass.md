# or_propclass.csv — Oregon property class codes

**Official source:** OAR 150-308-0310 *Real Market Value and Property Classification as Part of
Assessment Roll*, section (8) "Definitions for Property Classification System" and its attachment
"Basic Property Classes" (Oregon Secretary of State administrative rules database,
https://secure.sos.state.or.us/oard/view.action?ruleNumber=150-308-0310 — classification table in
the rule's PDF attachment, viewAttachment.action?ruleVrsnRsn=252769, accessed 2026-06-12).

**Code structure (the rule is compositional, so this table is a full mechanical enumeration —
1,000 three-digit codes, every label composed verbatim from the rule's digit definitions):**
- **First digit = basic class:** 0 Miscellaneous, 1 Residential, 2 Commercial, 3 Industrial,
  4 Tract, 5 Farm, 6 Forest, 7 Multi-family, 8 Recreation, 9 Exempt.
- **Second digit (classes 1–8):** 0 no significance; 1/2/3 nonconforming zoning (res/com/ind
  zone); 4 special assessment by application (non-EFU farm or designated forestland); 5 EFU;
  6 Small Tract Forestland; 7 permanently disqualified farm use (ORS 215.236); 8 multiple special
  assessments; 9 potential development.
- **Third digit (classes 1–8):** 0 vacant, 1 improved, 2 condominium, 3 state responsibility,
  4 partially exempt, 5 taxable leased, 6 waterfront, 7 mobile home park, 8 unassigned,
  9 manufactured structure.
- **Class 0 (miscellaneous)** reuses the second digit as the *related* basic class (0-5-x =
  miscellaneous farm) with its own third-digit table (mineral interest, centrally assessed,
  historic, open space, timeshare, enterprise zone...).
- **Class 9 (exempt)** uses the second digit as *ownership type* (0 student housing, 1 church,
  2 school, 3 cemetery, 4 city, 5 county, 6 state, 7 federal, 8 benevolent/fraternal, 9 port/other
  municipal) with its own third-digit table.

Verified against the dataset: every distinct `land_use` value sampled from the 33 ingested OR
counties (`550`, `970`, `101`, `471`, `141`, `680`, `109`, `940`...) parses under this table.
The rule lets a county deviate from the basic classes **only with written DOR approval**, so
unmatched county-local codes are possible but should be rare — leave them unmapped (honest null)
rather than guessing.

**Category mapping** (8-bucket vocabulary shared with `wa_dor.csv`): driven by the FIRST digit —
1/7 → residential, 2 → commercial-trade (OR doesn't split trade vs services; same documented
choice as TX F1), 3 → industrial, 4 (tract = suburban/rural homesite) → residential,
5/6 → agriculture/forest, 8 → recreation/cultural. Class 0 takes the category of its related
basic class (second digit). Class 9 (exempt) maps by ownership type to the WA analogs: churches
and benevolent/fraternal → recreation/cultural (WA 72), schools/cemeteries/city/county/state/
federal → commercial-services (WA 67/68 governmental precedent — note BLM/USFS holdings surface
here as commercial-services because the class records ownership, not use), ports → transport/
utilities (WA 44), student housing → residential (WA 17). Code `xx8`/`9x7`/`0x6` "unassigned"
third digits are included (labelled "(third digit not assigned)") to mirror wa_dor.csv's
"Not presently assigned" rows.

**Parser note:** codes are three digits; a county exporting fewer digits (e.g. `4`) denotes the
bare class — zero-PAD RIGHT is wrong; treat 1–2 digit values as class prefixes (first digit
already determines the category). The WA two-digit regex in `landuse_std._extract_code` does not
apply to OR.
