# National Parcel Coverage

Every US county keeps a public record of who owns each piece of land. Most publish it
online, each in its own format, on its own website. Commercial copies of the whole
country exist, and they are priced for companies.

This project asks how much of that national record can be put together from free public
sources alone. As of the July 2026 build the answer is **143,955,093 parcel records from
2,613 of 3,143 US counties (83%)**, in one schema, with the source recorded for every county.

## See it

The county coverage map is at <https://aschulz714.github.io/us-parcel-coverage/> (or open
`docs/index.html` from a copy of this repository). It shows, county by county, whether the
record could be collected, where it came from, and how often each field is filled.

Zoom in on a county and the map draws its parcel outlines. Click one for its parcel number
and, where the county's server has them, its land use, value, size, year built, zoning and
last sale. All of that comes live from the county's own public server, at the address
recorded in the catalog, not from the store.

King County, Washington is the one exception to "the address recorded in the catalog". The
layer the catalog names for it carries the parcel number and nothing else, so the map reads
the county's own property-information layer on the same server instead, for land use, land
and building value, size and zoning. No owner or address field is asked for there. The
substitution is `MAP_ONLY_SOURCE` in `tools/make_coverage_map_data.py`.

A click also asks that server who owns the one parcel clicked, by its parcel number. Where
the request cannot be narrowed to that one parcel, the page does not ask. The owner's name and the
parcel's street address are shown only when the name plainly reads as an organization, such
as a company, a public body or a church. Any other name is dropped in the browser as it
arrives: it is never shown and never kept. The rule leans toward hiding, so it also hides
organizations it does not recognize, and a company named after a person still shows as a
company. New Jersey counties are not asked for owners at all
(`config/publication_policy.json`). The rule is in `docs/index.html` between the
`owner-rule` markers.

A county draws only while its server still answers at that address and allows other websites
to read it.

## What is in this repository

| Folder | What it holds |
|---|---|
| `src/` | The pipeline: pull a county's parcels, put them in one schema, standardize owners and land use, clean values, build map tiles, back up. Also the live owner lookup (`resolve.py`). |
| `adapters/` | Readers for the common county map-service types. |
| `qa/` | Regression tests and the validation suite. |
| `config/` | The publication policy: which fields may be published, state by state. |
| `data/parcels/manifest.json` | One entry per county in the store: source address, field mapping, record count, fill rates, and dates. |
| `src/county_methods.csv` | One row per county or county-equivalent (3,230 rows: 3,221 county-equivalents plus 9 alias rows for Connecticut's planning regions): how its owner record can be reached, or why it cannot. |
| `data/landuse_codes/` | Land-use code tables by state. |
| `docs/` | The coverage map, the land-use taxonomy, and the release-safety rules. |
| `tools/` | The script that builds the map's data file: county totals, each county's public source address and the names of its fields. No records. |

## What is not here

- **The parcel records themselves.** This is a code and catalog release. The repository
  holds no parcel record, owner name, or street address. The coverage map reads the fields
  described above live from county servers and keeps none of them. `docs/RELEASE_SAFETY.md`
  describes the checks a data release has to pass first, including source terms and privacy
  review.
- **Per-county research notes and probe evidence.** They are held back until they have been
  reviewed for personal information.
- **Sources known to prohibit redistribution.** They were kept out of the store. The rest
  still need a rights review before any data release.

Some scripts and tests expect files that are not in this release, so the pipeline does not
run end to end from this copy.

## How it was built

AI agents searched every state for public sources in parallel and wrote most of the code.
Every source they found was then checked by hand with a live query before it was trusted,
because the agents both missed sources and oversold others. Each pull is checked against
the source's own record count where the source reports one.

## The pipeline, per county or state

```bash
python src/ingest.py --state WA          # pull full parcel layers
python src/normalize.py --state WA       # one standard schema
python src/owners_std.py --state WA      # standardized owner names
python src/mailing_std.py --state WA     # standardized mailing addresses
python src/landuse_std.py --state WA     # land-use codes and categories
python src/value_clean.py --fips 53069   # remove placeholder values
python qa/validate.py --state WA         # regression suite
python src/build_tiles.py --state WA     # map tiles
```

Every county ends up in the same schema: state, county_fips, county_name, apn, owner,
owner_norm, owner_family, situs_address, mailing_address, mailing_norm, mail_city, mail_state,
mail_zip, assessed_value, land_value, improvement_value, land_use, land_use_code,
land_use_category, land_use_desc, zoning, year_built, building_sqft, sale_price, sale_date,
acreage, source_url, source_vintage, retrieved_at, geometry.

## Limits

- Counts above are from the July 2026 build. `data/parcels/manifest.json` is newer (September
  2026) and counts rows as pulled, before cleanup: its 2,613 entries sum to 144,005,324.
- A source date is recorded for 901 of the 2,613 counties in the store. Those dates run from 2017 to 2026.
- A filled field is not a verified field. Fill rates measure presence, not correctness.
- Puerto Rico's 78 municipios are recorded but are outside the store.

## License

The code is released under the MIT License (see `LICENSE`). County and state source data
remains under each source's own terms.
