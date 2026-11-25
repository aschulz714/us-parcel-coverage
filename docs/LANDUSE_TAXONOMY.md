# National land-use taxonomy (`land_use_category`)

**Built 2026-06-28.** One standardized land-use vocabulary across every county, written as the
`land_use_category` column on each normalized parcel (the `land_use_std` deliverable). Tool:
`src/landuse_std.py`. Raw `land_use` / `land_use_desc` are preserved untouched — this column
*standardizes*, never overwrites.

## The vocabulary (8 SLUCM-style buckets)

`residential` · `commercial-trade` · `commercial-services` · `industrial` · `agriculture/forest`
· `recreation/cultural` · `transport/utilities` · `undeveloped/open`

These are the categories already used by the 15 per-state code tables in `data/landuse_codes/`.
`recreation/cultural` is also the **institutional / public / exempt** bucket (church, school,
government, park, cemetery). A null `land_use_category` means *we could not classify honestly* —
either the county publishes no raw land use, or the value is an unrecognized code (we never guess
a code's meaning).

> For the two headline use cases: **all residential** = `land_use_category = 'residential'`;
> **vacant / land plays** = `land_use_category = 'undeveloped/open'`. Note: a parcel labeled
> "Vacant Residential" classifies as `residential` (intended use, matching FL DOR 000) — to find
> *physically* undeveloped land, combine with `improvement_value = 0` rather than relying on the
> category alone.

## Method — a per-parcel waterfall

1. **State code table** (precise, per-state). 15 states wired in `STATE_TABLES`: FL, TX, OR, TN,
   AR, ID, MT, NC, WI, WV, OH, NY, MA, UT (+ WA via DOR parse / statewide join). Each table maps
   the state's official code → category with the right parse rule (3-digit zero-pad, longest-prefix
   for CAD suffixes, first-digit, multiclass, exact-text, …). Sources cited in each `*.md`.
2. **National keyword classifier** (`classify_text`) over `land_use`, then `land_use_desc`. Ordered
   exact-ish keyword rules → the same 8 buckets ("SINGLE FAMILY"→residential, "VACANT LAND"→
   undeveloped/open, "AGRICULTURAL"→agriculture/forest, generic "COMMERCIAL"→commercial-trade).
   **Numerics never keyword-match, so a code is never guessed.** Ordering handles compound labels
   (`MOBILE HOME PARK`→residential not park; `VACANT RESIDENTIAL`→residential; `NON-RES` neutralized
   so it can't false-fire residential). Precision audited against the national value head.

The keyword tier is the piece the tool's docstring always promised but never had; it is what lets
the ~22 states with no code table get a category from readable text, and fills code-table states'
text-valued / untabled rows too.

## Coverage (measured on the written data after the national run, 2026-06-28)

- 114.2M total parcels; 64.4M have a raw land_use (56.4%).
- **`land_use_category` populated on 52.3M parcels = 45.8% of all parcels / 81.2% of the parcels
  that HAVE a raw land_use.** The remaining 43.6% of all parcels have **no raw land_use at all** —
  a source-data gap (see backlog B), not a taxonomy gap.
- National distribution: residential 36.6%, undeveloped/open 3.2%, agriculture/forest 2.7%,
  commercial-trade 1.4%, commercial-services 1.0%, recreation/cultural 0.4%, industrial 0.3%,
  transport/utilities 0.2%.
- Strong states: FL/MA/WI/RI 100%, ID 99%, DE 99%, OR 98%, OH 96%, NC 95%, TX 92%, NY 80%,
  CO 81%, MN 78%, NM 71%, IN/MD 68%. National run: 2,061 counties, 0 failures.

## Backlog — how to push coverage higher

**A. Add a state code table** (low-null states whose unrecognized values are a real numeric scheme —
each needs its scheme *verified against an authoritative source* before adding; do not guess codes):

| State | null% | top unrecognized | note |
|-------|------:|------------------|------|
| NJ | 17% | `2` (560k) | NJ MOD-IV property class (1 vacant /2 res /3A-B farm /4A-C comm-ind-apt /5 rr /6 util /15 exempt) — well-documented statewide standard, **highest-value add** |
| NH | 0% | `1010` (146k) | 4-digit scheme, verify NH assessing codes |
| TN | 13% | `1` (2.25M) | non-Hamilton counties publish 1-digit codes — verify (SLUCM 1-digit vs county scheme) |
| MI | 58%* | `401` (406k) | MI assessment class 1xx ag /2xx comm /3xx ind /4xx res — verify before trusting first-digit |
| GA, MO, IN, MN, NM, IL | varies | `101`/`110`/`510`/`100`/`1100`/`0040` | each a numeric scheme; verify per state |

**B. Re-ingest for missing `land_use`** (states ~100% null — nothing to classify; the fix is fetching
a land-use field at ingest, not taxonomy): LA, MS, ND, OK, WY (100%), KS (98%), AL (95%), and the
ODNR-sourced OH counties (93% of OH rows). Lower-null re-ingest candidates: AZ (89%, masked→nulled),
IA (80%), SD (79%).

**C. Known un-fixable / honest nulls:** VT `PARCEL` placeholder, MT `Improved Property` /
UT `Unknown` (carry no use), VA Chesterfield (`land_use` is **mis-mapped at ingest** — holds
value/sqft-like numbers, not codes; needs a re-ingest with the correct field, tracked separately).

## Re-run

```
python src/landuse_std.py            # all counties (idempotent; pyarrow, geometry preserved)
python src/landuse_std.py --state NJ # one state after adding its code table
```

`land_use_category` + `land_use_code` are regenerable from raw `land_use` → the R2 backup is
stale-by-two-columns but needs no urgent re-push; they are tile fields, so a future re-tile can
surface category on the map.
