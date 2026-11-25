# qa/ — dataset validation suite

A repeatable regression suite so every future refresh and every new state gets the same
scrutiny the first 12 states got by hand. Built 2026-06-12 against the 12-state /
48.4M-parcel dataset; every ground-truth row and every baseline number was verified
passing against the live data on that date.

## Files

| file | what it is |
|---|---|
| `validate.py` | the runner — check tiers A-D, exits nonzero on any failure |
| `ground_truth.csv` | verified owner facts (state, optional county pin, ILIKE pattern, floor) |
| `coverage_baseline.json` | frozen per-state/per-county coverage numbers from the manifest's `populated` audits |
| `dup_baseline.json` | frozen per-county duplicate-APN rates measured from the normalized data (2026-07-01) |
| `known_issues.csv` | pre-existing, investigated data bugs with an allowed count — the suite warns at the recorded size and FAILS if one grows or a new one appears |
| `FINDINGS.md` | what those known issues are, plus data oddities worth knowing (not failures) |
| `test_pipeline_synthetic.py` | synthetic-county integration test: a 10-row fake county through normalize → value_overlay → value_clean, asserting the historical silent-data-loss bug classes (comma-nulled values, overlay wipes, tax-named declared fields, all-9s sentinels, displaced geometry, sqft acreage) stay fixed. Self-contained (temp dir only); run it after ANY change to normalize.py / value_overlay.py / value_clean.py |

## Run it

```bash
python qa/validate.py                  # full run (~5-15 min at 2 threads)
python qa/validate.py --state TX       # just one state (after a single-state refresh)
python qa/validate.py --skip-geometry  # skip the bbox tier
python qa/test_pipeline_synthetic.py   # pipeline-code regression test (seconds, no real data)
python qa/test_geo_enrich.py           # offline parcel/tract row-identity regression tests
python qa/test_stage_tract_repair.py   # offline staging, resume, and failure-safety tests
python qa/test_oz_geography.py         # original-round OZ boundaries and staging regressions
python qa/test_promote_tract_repair.py # promotion safety: hash gates, idempotence, reclaim, refusals
python qa/test_normalize_sdat_family.py # MD SDAT field mapping + degenerate-APN overrides (synthetic raws)
python qa/test_refresh_durability.py   # staged-generation refresh: fail-closed validation, fips injection, dispatch
python qa/test_raw_inventory.py        # raw inventory + row-reconciliation evidence tiers (local / archived / normalized)
python qa/validate.py --write-dup-baseline  # re-freeze duplicate-APN rates (verified data only)
```

Defaults are deliberately light (`memory_limit=2GB`, `threads=2`) so it can run while
ingests/tilers are running; raise with `--memory-limit 8GB --threads 8` on an idle machine.
Exit code 0 = pass, 1 = failures (summary at the bottom of the report), 2 = setup error.

## The three tiers and what a failure means

### A. GROUND TRUTH — `ground_truth.csv`
Each row is a publicly verifiable fact (Disney owns hundreds of Orange/Osceola FL parcels,
PotlatchDeltic owns north-Idaho timberland, ...) expressed as: `owner ILIKE pattern`
(within `county_fips` if pinned) must match at least `min_parcels` rows. 85 rows across
all 12 states (6-8 per state); every one was verified passing on 2026-06-12, with the
observed count recorded in its note. Floors are set at roughly **half** the observed
count, so ordinary sales/splits never trip them.

A failure usually means one of:
- a refresh pulled a **wrong/empty owner field** for that county (the Klamath/Yakima class),
- a source **moved or truncated** (the Jackson OR 22,000-row AGOL mirror class),
- a normalize change broke `owner` for a state,
- (rarely) the entity genuinely divested — verify in the raw source, then update the row
  and note the change in FINDINGS.md.

Patterns match the **raw `owner` column**, not `owner_norm`, so they keep working even if
the standardization rules change. Patterns were tightness-tested against the live data
(e.g. `NIKE INC%` not `%NIKE%`, which also matches SETNIKER FARM LLC; `MAR A LAGO CLUB%`
not `%MAR%A%LAGO%`, which matches MARIN WENDY MALAGON).

### B. COVERAGE FLOORS — vs `coverage_baseline.json`
Per state, owner/apn/assessed_value coverage (fraction of rows non-null, the same
definition `normalize.py` records in the manifest's `populated` audits) must not drop more
than **2 percentage points** below the frozen baseline. This is a regression check against
the known-good numbers, NOT an absolute threshold — states legitimately differ (many OR
counties publish no values at all; that is baked into the baseline, not a failure).

Why a frozen file instead of reading the manifest live: **every normalize run rewrites the
manifest's `populated` numbers.** If a refresh broke owner coverage, the manifest would
record the broken numbers and a manifest-vs-data comparison would happily pass. The
baseline file freezes the verified-good state. Only regenerate it
(`python qa/validate.py --write-baseline`) after a refresh has been reviewed and accepted.

Also failed here: any county that exists in the baseline but is missing/empty on disk
(a refresh that silently dropped a county). New counties (state expansion) are reported
informationally and enter the baseline at the next `--write-baseline`.

### C. SANITY — invariants that should never break
- **No `assessed_value` > $20B** (raised from $5B for NYC mega-parcels: JFK assesses at
  $8.5B). Anything above is a parse error (parcel numbers landing in value columns —
  the Yakima ASSESSOR_N class).
- **Every normalized file carries the required columns** (incl. `owner_norm`,
  `land_use_category`). Those two are written by the overlay passes (owners_std /
  landuse_std), NOT by normalize — a re-normalize without the follow-up passes silently
  drops the county from any national query touching them (the 49-file / ~3.2M-parcel
  schema-drift incident, found 2026-07-01). Metadata-only check, costs nothing.
- **Per-county duplicate-APN rate must not grow >2pts past `dup_baseline.json`**
  (and >1,000 excess rows). Catches duplicate-ROW regressions Tier D's static check
  can't see: double-append ingests, fan-out joins in backfills. Condo/common-area
  counties legitimately have high absolute rates — that's baked into the baseline;
  only growth fails.
- **No whitespace/empty `owner_norm`.** Standardization must produce NULL or a real
  string; `''` means a cleaning rule regressed.
- **Pipeline sentinels always bracketed.** Lawful-redaction placeholders
  (`[SUPPRESSED - RCW...]`, `[NOT PUBLISHED - ...]`, `[INDIVIDUAL OWNER - ...]`) must
  start with `[` and close with `]`; a bare `INDIVIDUAL OWNER` / `NOT PUBLISHED` means an
  ingest path skipped the rewrite and rankings/research would treat it as a real owner.
- **`county_fips` valid** — non-null, present in `src/county_methods.csv` AND in the
  manifest (catches fips-format drift in statewide-slicer `where` clauses).
- **No geometry bbox spanning > 5° and none outside its state's lon/lat window** — the
  world-spanning degenerate-ring bug (Lincoln WA class) plus the compact-but-displaced
  variant (El Paso parcels sitting in the Pacific — small span, wrong hemisphere — which
  a span rule alone cannot see). Checked cheaply: a file's geoparquet bbox metadata
  bounds every row's bbox, so only suspect files get a per-row drill (DuckDB spatial
  extension). New states need a `STATE_WINDOW` entry in validate.py (the suite warns).

Tier C failures route through `known_issues.csv`: an investigated, documented issue at
or below its recorded count reports as a warning; growth past the allowance or any new
occurrence fails. Never raise an allowance without re-investigating and updating
FINDINGS.md.

## Hooking into refresh runs (future; documentation only)

The intended flow once wired in (do NOT edit `refresh.py` until the lead session does it):

1. `python src/refresh.py --check ...` → decide what is stale
2. `python src/refresh.py --pull ...` → re-ingest + re-normalize the stale set
3. `python qa/validate.py` (or `--state XX` for a single-state pull) → **gate**
4. on pass → rebuild tiles/map, commit the new manifest, and `--write-baseline` to adopt
   the new coverage numbers
5. on fail → the report names the county/check; fix or roll back the pull before anything
   ships. Genuine entity changes (a ground-truth owner really divested) get the CSV row
   updated in the same commit, with a line in FINDINGS.md.

For a NEW state: ingest + normalize as usual, add a handful of researched owner facts for
it to `ground_truth.csv` (verify the counts first — this file only holds facts that PASS),
run `validate.py --state XX`, then `--write-baseline` to adopt the state.

## Conventions for adding ground-truth rows

- Research owners that are **large, institutional, and durable**: universities, utilities,
  railroads, state land trusts, flagship corporate campuses. Avoid anything likely to be
  sold/renamed within a few years.
- **Verify the pattern against the data before adding it** — check what it actually
  matches (`SELECT owner_norm, count(*) ... WHERE owner ILIKE pattern GROUP BY 1`), not
  just the count. Loose patterns match strangers (`%NIKE%` → SETNIKER FARM LLC).
- Set `min_parcels` ≈ half the observed count (floor 1 for iconic single parcels like
  Mar-a-Lago).
- Pin `county_fips` when the fact is county-specific (campuses, HQ towns); leave it empty
  for statewide facts (railroads, utilities, timber).
- Record the observed count and date in `note`, and the public-knowledge basis in `source`.
