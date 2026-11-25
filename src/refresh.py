"""REFRESH SCHEDULER — keep the local dataset current without re-downloading the country.

For every county in data/parcels/manifest.json, decides STALE vs CURRENT using the cheapest
reliable signal, most-trustworthy first:

  1. layer editingInfo.lastEditDate  (one metadata GET) ........ edited since our pull -> STALE-EDIT
  2. bulk-source Last-Modified HEAD  (King/Pierce extract zips)  newer than our pull   -> STALE-SOURCE
  3. live feature count vs manifest  (one count GET) ........... changed              -> STALE-COUNT
  4. age fallback (no verifiable signal) ....................... older than --max-age  -> STALE-AGE

A county whose lastEditDate is OLDER than our pull is verified CURRENT — no re-download,
which is what keeps refresh proportional to actual change instead of dataset size.
--pull re-ingests + re-normalizes only the stale set; --min-parcels/--limit implement the
metro-first tiering (rank by parcel count, biggest first).

NOTE for national scale-up: counties sharing one STATEWIDE layer (TX/ME/PR class) get the
layer-level lastEditDate check (correct), but the count check is skipped for them — a live
layer-wide count is not comparable to a per-county count. They fall to the age policy.

Usage:
  python src/refresh.py                          # check everything, report (no downloads)
  python src/refresh.py --state WA --max-age 90
  python src/refresh.py --pull                   # re-pull + re-normalize all STALE counties
  python src/refresh.py --pull --min-parcels 100000 --limit 3   # metros first, capped
  python src/refresh.py --pull --force-fips 53069               # manual override / testing
"""
import argparse, concurrent.futures as cf, datetime, glob, json, os, sys

import requests

sys.path.insert(0, os.path.dirname(__file__))
import ingest
import landuse_std
import normalize
import owners_std
import ingest_bulk_wa as bulk

HERE = os.path.dirname(__file__)
ROOT = os.path.dirname(HERE)
PARCELS = os.path.join(ROOT, "data", "parcels")
MANIFEST = os.path.join(PARCELS, "manifest.json")
UA = ingest.UA

BULK_FIPS = {"53033": "king", "53053": "pierce", "53011": "clark",
             "53027": "grays_harbor", "53049": "pacific",
             "53001": "adams", "53023": "garfield", "53043": "lincoln",
             "41041": "lincoln_or", "41005": "clackamas", "41057": "tillamook", "41049": "morrow"}


def _parse_iso(s):
    if not s:
        return None
    try:
        return datetime.datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=datetime.timezone.utc)
    except ValueError:
        return None


def _meta(url):
    try:
        r = requests.get(url, params={"f": "json"}, headers=UA, timeout=45)
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


def _live_count(query_url):
    try:
        r = requests.get(query_url, params={"where": "1=1", "returnCountOnly": "true", "f": "json"},
                         headers=UA, timeout=45)
        r.raise_for_status()
        return r.json().get("count")
    except Exception:
        return None


def _bulk_last_modified(county_key):
    """Newest Last-Modified across the county's bulk source zips (None if none expose it)."""
    newest = None
    for url in bulk.BULK_SOURCES.get(county_key, {}).values():
        try:
            r = requests.head(url, headers=UA, timeout=45, allow_redirects=True)
            lm = r.headers.get("Last-Modified")
            if lm:
                t = datetime.datetime.strptime(lm, "%a, %d %b %Y %H:%M:%S %Z").replace(
                    tzinfo=datetime.timezone.utc)
                newest = max(newest, t) if newest else t
        except Exception:
            pass
    return newest


def check_county(entry, shared_endpoint_fips, max_age_days):
    """Return (status, detail). Statuses: STALE-EDIT, STALE-SOURCE, STALE-COUNT, STALE-AGE,
    CURRENT, UNVERIFIED, CHECK-FAILED."""
    fips = entry["fips"]
    pulled = _parse_iso(entry.get("retrieved_at"))
    if not pulled:
        return "CHECK-FAILED", "no retrieved_at in manifest"
    age_days = (datetime.datetime.now(datetime.timezone.utc) - pulled).days
    base = entry["source_url"].rsplit("/query", 1)[0]

    # Signal 1: layer edit date (layer-level, so valid even on shared statewide layers)
    meta = _meta(base)
    if meta and (meta.get("editingInfo") or {}).get("lastEditDate"):
        edited = datetime.datetime.fromtimestamp(
            meta["editingInfo"]["lastEditDate"] / 1000, datetime.timezone.utc)
        if edited > pulled:
            return "STALE-EDIT", f"layer edited {edited.date()} > pulled {pulled.date()}"
        return "CURRENT", f"no edits since pull ({edited.date()} <= {pulled.date()})"

    # Signal 2: bulk extract Last-Modified (the King/Pierce owner sources)
    if fips in BULK_FIPS:
        lm = _bulk_last_modified(BULK_FIPS[fips])
        if lm and lm > pulled:
            return "STALE-SOURCE", f"bulk extract modified {lm.date()} > pulled {pulled.date()}"
        if lm:
            return "CURRENT", f"bulk extracts unchanged since pull ({lm.date()})"

    # Signal 3: live count vs what we recorded (single-county layers only)
    if meta is None:
        return "CHECK-FAILED", "layer metadata unreachable"
    if fips not in shared_endpoint_fips:
        ref = entry.get("reported_count") or entry.get("feature_count")
        live = _live_count(entry["source_url"])
        if live is not None and ref is not None:
            if int(live) != int(ref):
                return "STALE-COUNT", f"live count {live:,} != recorded {ref:,}"
            # unchanged count proves little for attribute edits -> age policy still applies below
            if age_days <= max_age_days:
                return "UNVERIFIED", f"count stable ({live:,}); age {age_days}d <= {max_age_days}d"

    # Signal 4: age fallback
    if age_days > max_age_days:
        return "STALE-AGE", f"no edit signal; age {age_days}d > {max_age_days}d"
    return "UNVERIFIED", f"no edit signal; age {age_days}d <= {max_age_days}d"


REFRESHABLE_METHODS = {"arcgis", "arcgis-join", "wfs"}   # every method ingest.ingest_county dispatches
GEN_DIR = os.path.join(PARCELS, "normalized_gen")          # same volume as normalized/ -> atomic promote
MIN_ROW_RETENTION = 0.95      # a fresh normalize may drop displaced-geometry junk, never 5%+ of a county
MAX_DUP_GROWTH = 0.02         # mirrors qa/validate.py tier C: duplicate-APN rate may not grow > 2 points
MAX_COVERAGE_DROP = 0.05      # owner / apn / value population may not fall > 5 points on a refresh
# A previous generation under 1% of the new one's rows is a stub, not a baseline (Malheur OR held
# 1 row before its 20,570-parcel repoint, so its 0% duplicate rate rejected every real pull):
# nothing is compared against it, and the new generation's own duplicate rate is capped instead.
DEGENERATE_PREVIOUS = 0.01
MAX_FIRST_DUP_RATE = 0.25
REQUIRED_DERIVED = ("owner_norm", "owner_family", "land_use_category", "mailing_norm",
                    "census_tract", "opportunity_zone_2018")
NORMALIZED_SIDE_KEYS = {"normalized_file", "field_map", "populated", "normalized_row_count",
                        "normalization_dropped_count", "grain_dropped_count",
                        "landuse_method", "landuse_coverage",
                        "refreshed_at", "refresh_checks"}


def load_entries(manifest):
    """Manifest entries with the manifest KEY injected as 'fips' (bulk-builder entries lack the
    inner field: 41041 / 53027 / 53049 crashed the national refresh with KeyError before 2026-09-14)."""
    return [dict(e, fips=fips) for fips, e in manifest.items() if e.get("normalized_file")]


def geo_enrich_fn(entry):
    """Re-derive census_tract / opportunity_zone(_2018) / owner_occupied on entry['normalized_file'].
    Module-level so offline tests can substitute a stub (the real one needs TIGER + the OZ archive)."""
    import geo_enrich
    geo_enrich.process([entry])


class RefreshRejected(RuntimeError):
    """The staged generation failed validation; the canonical file was NOT touched."""


def _profile(path):
    import pyarrow.parquet as pq
    import pyarrow.compute as pc
    import pyarrow as pa
    schema = pq.read_schema(path)
    want = [c for c in ("apn", "owner", "assessed_value") if c in schema.names]
    t = pq.read_table(path, columns=want)
    n = t.num_rows
    prof = {"rows": n, "columns": set(schema.names)}
    for c in want:
        col = t[c]
        prof[c] = 0.0 if (not n or pa.types.is_null(col.type)) else (n - col.null_count) / n
    if "apn" in want and n and not pa.types.is_null(t["apn"].type):
        apn = t["apn"].drop_null()
        prof["dup_rate"] = (len(apn) - pc.count_distinct(apn).as_py()) / n
    else:
        prof["dup_rate"] = 0.0
    return prof


def validate_generation(staged, canonical, source_rows):
    """Fail closed. Returns the check dict on success; raises RefreshRejected with the first failure."""
    new = _profile(staged)
    old = _profile(canonical) if canonical and os.path.exists(canonical) else None
    checks = {"rows": new["rows"], "source_rows": source_rows}
    if new["rows"] == 0:
        raise RefreshRejected("staged generation has 0 rows")
    if source_rows and new["rows"] < MIN_ROW_RETENTION * source_rows:
        raise RefreshRejected(f"row retention {new['rows']:,}/{source_rows:,} "
                              f"= {new['rows'] / source_rows:.1%} < {MIN_ROW_RETENTION:.0%}")
    missing = [c for c in ("apn", "owner", "geometry", *REQUIRED_DERIVED) if c not in new["columns"]]
    if missing:
        raise RefreshRejected(f"staged generation lacks required columns: {missing}")
    if old and old["rows"] < DEGENERATE_PREVIOUS * new["rows"]:
        checks.update(previous_rows=old["rows"], degenerate_previous=True,
                      dup_rate=(None, round(new["dup_rate"], 4)))
        if new["dup_rate"] > MAX_FIRST_DUP_RATE:
            raise RefreshRejected(f"duplicate-APN rate {new['dup_rate']:.1%} > {MAX_FIRST_DUP_RATE:.0%} "
                                  f"(previous generation is a {old['rows']:,}-row stub, not a baseline)")
        old = None
    if old:
        checks["previous_rows"] = old["rows"]
        if old["rows"] and new["rows"] < MIN_ROW_RETENTION * old["rows"]:
            # a source that silently shrank (truncated layer, partial pull that reconciled to its
            # own short count) must not replace a fuller previous generation without a human look
            raise RefreshRejected(f"row retention vs previous generation {new['rows']:,}/{old['rows']:,} "
                                  f"= {new['rows'] / old['rows']:.1%} < {MIN_ROW_RETENTION:.0%}")
        checks["dup_rate"] = (round(old["dup_rate"], 4), round(new["dup_rate"], 4))
        if new["dup_rate"] > old["dup_rate"] + MAX_DUP_GROWTH:
            raise RefreshRejected(f"duplicate-APN rate grew {old['dup_rate']:.1%} -> {new['dup_rate']:.1%}")
        for c in ("apn", "owner", "assessed_value"):
            if c in old and c in new:
                checks[c] = (round(old[c], 3), round(new[c], 3))
                if new[c] < old[c] - MAX_COVERAGE_DROP:
                    raise RefreshRejected(f"{c} population fell {old[c]:.1%} -> {new[c]:.1%}")
    return checks


def build_generation(entry):
    """Normalize + re-derive EVERY dependent column into a staged file under GEN_DIR, then validate
    it against the source count and the current canonical file. Never writes normalized/."""
    fips = entry["fips"]
    gen_dir = os.path.join(GEN_DIR, fips)
    os.makedirs(gen_dir, exist_ok=True)
    for stale in glob.glob(os.path.join(gen_dir, "*.parquet")):
        os.remove(stale)
    norm_rel, fmap, populated = normalize.normalize_county(entry, gen_dir)
    staged = os.path.join(ROOT, norm_rel)
    staged_entry = dict(entry, normalized_file=norm_rel)
    method, coverage = landuse_std.apply_county(staged_entry)
    aliases, families = owners_std.load_config()
    owners_std.apply_files([staged], aliases, families)
    import mailing_std
    mailing_std.apply_files([staged])
    geo_enrich_fn(staged_entry)          # any failure surfaces via the required-column check below
    canonical = os.path.join(PARCELS, "normalized", os.path.basename(staged))
    source_rows = int(entry.get("fetched_count") or entry.get("feature_count") or 0)
    # A declared parcel-grain collapse (normalize.GRAIN_KEYS: join-grain source layers) is a
    # documented drop, not a truncation — the retention gate compares against the grain count.
    grain_dropped = int(normalize.GRAIN_DROPS.get(fips, 0))
    checks = validate_generation(staged, canonical, max(0, source_rows - grain_dropped))
    checks["grain_dropped"] = grain_dropped
    return {"staged": staged, "canonical": canonical, "norm_rel": norm_rel, "fmap": fmap,
            "populated": populated, "landuse_method": method, "landuse_coverage": coverage,
            "source_rows": source_rows, "grain_dropped": grain_dropped, "checks": checks}


def promote_generation(gen):
    """Atomic same-volume replace of the canonical file, then ONE manifest update with everything
    the generation established. The previous generation is gone only after os.replace succeeds."""
    from atomic_io import atomic_replace_writer
    import pyarrow.parquet as pq
    atomic_replace_writer(gen["canonical"], lambda temp: os.replace(gen["staged"], temp))
    rows = pq.read_metadata(gen["canonical"]).num_rows
    fips = os.path.basename(gen["canonical"])[:5]
    ingest.update_manifest(fips, {
        "normalized_file": os.path.relpath(gen["canonical"], ROOT).replace("\\", "/"),
        "field_map": gen["fmap"], "populated": gen["populated"], "normalized_row_count": rows,
        "normalization_dropped_count": max(0, gen["source_rows"] - rows),
        "grain_dropped_count": int(gen.get("grain_dropped") or 0),
        "landuse_method": gen["landuse_method"], "landuse_coverage": gen["landuse_coverage"],
        "refreshed_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "refresh_checks": gen["checks"]}, MANIFEST)
    try:
        os.rmdir(os.path.dirname(gen["staged"]))
    except OSError:
        pass


def pull_county(entry, crosswalk):
    """Re-ingest one county, build its next generation in staging (normalize + land-use + owner +
    mailing + geo re-derivation), validate, and only then promote. A failure at any step leaves the
    current canonical file and manifest entry untouched and raises (main exits nonzero)."""
    fips = entry["fips"]
    if fips in BULK_FIPS:
        key = BULK_FIPS[fips]
        bulk.download_sources(key, force=True)
        bulk.pull_geometry(key, refresh=True)
        bulk.BUILDERS[key]()           # bulk builders write normalized/ themselves (legacy path)
        manifest = ingest.load_manifest(MANIFEST)
        fresh = dict(manifest[fips], fips=fips)
        landuse_std.apply_county(fresh)
        aliases, families = owners_std.load_config()
        files = sorted(glob.glob(os.path.join(PARCELS, "normalized", f"{fips}*.parquet")))
        owners_std.apply_files(files, aliases, families)
        import mailing_std
        mailing_std.apply_files(files)
        geo_enrich_fn(fresh)
        return
    row = crosswalk.get(fips)
    if not row or row.get("method") not in REFRESHABLE_METHODS:
        raise RuntimeError(f"{fips}: crosswalk method {row.get('method') if row else None!r} is not "
                           f"refreshable here (supported: {sorted(REFRESHABLE_METHODS)})")
    new_entry = ingest.ingest_county(row, os.path.join(PARCELS, "raw"), refresh=True)
    if not new_entry:
        raise RuntimeError(f"{fips}: ingest returned no entry")
    # raw provenance first (lockfile-guarded). Normalized-side fields are owned by promote_generation
    # and must never be touched before validation passes, whatever an adapter happens to return.
    raw_side = {k: v for k, v in new_entry.items() if k not in NORMALIZED_SIDE_KEYS}
    ingest.update_manifest(fips, raw_side, MANIFEST)
    try:                                    # QA reconciles raw rows from this inventory once the
        import raw_inventory                # local raw is deleted after its R2 push
        if new_entry.get("file"):
            raw_inventory.record(os.path.join(ROOT, new_entry["file"]))
    except Exception as exc:                # inventory is evidence, never a reason to abort a pull
        print(f"  {fips}: raw_inventory WARN {type(exc).__name__}: {exc}")
    manifest = ingest.load_manifest(MANIFEST)
    gen = build_generation(dict(manifest[fips], fips=fips))
    promote_generation(gen)
    print(f"  {fips}: promoted generation ({gen['checks']['rows']:,} rows; checks {gen['checks']})", flush=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--state")
    ap.add_argument("--fips", nargs="*", help="restrict the check/pull to these counties")
    ap.add_argument("--max-age", type=int, default=90,
                    help="days before a no-signal county counts as stale (default 90)")
    ap.add_argument("--pull", action="store_true", help="re-pull + re-normalize the stale set")
    ap.add_argument("--min-parcels", type=int, default=0, help="tiering: only counties this big")
    ap.add_argument("--limit", type=int, help="max counties to pull this run (biggest first)")
    ap.add_argument("--force-fips", nargs="*", default=[], help="treat these fips as stale regardless")
    args = ap.parse_args()

    manifest = ingest.load_manifest(MANIFEST)
    entries = load_entries(manifest)
    if args.state:
        entries = [e for e in entries if e["state"].upper() == args.state.upper()]
    if args.fips:
        entries = [e for e in entries if e["fips"] in set(args.fips)]

    # counties sharing one endpoint (statewide-layer class): count check is not comparable
    by_base = {}
    for e in entries:
        by_base.setdefault(e["source_url"].rsplit("/query", 1)[0], []).append(e["fips"])
    shared = {f for fipses in by_base.values() if len(fipses) > 1 for f in fipses}

    print(f"Checking {len(entries)} counties ({len(by_base)} unique endpoints, max-age {args.max_age}d)...")
    results = {}
    with cf.ThreadPoolExecutor(max_workers=12) as ex:
        futs = {ex.submit(check_county, e, shared, args.max_age): e for e in entries}
        for fut in cf.as_completed(futs):
            e = futs[fut]
            results[e["fips"]] = fut.result()
    for fips in args.force_fips:
        if fips in results:
            results[fips] = ("STALE-FORCED", "forced via --force-fips")

    order = {"STALE-EDIT": 0, "STALE-SOURCE": 0, "STALE-COUNT": 0, "STALE-AGE": 0, "STALE-FORCED": 0,
             "CHECK-FAILED": 1, "UNVERIFIED": 2, "CURRENT": 3}
    rows = sorted(entries, key=lambda e: (order[results[e["fips"]][0]], -(e.get("feature_count") or 0)))
    for e in rows:
        st, detail = results[e["fips"]]
        print(f"  {st:<13} {e['fips']} {e['county'][:16]:<16} {e['state']} "
              f"{(e.get('feature_count') or 0):>9,}  {detail}")
    stale = [e for e in rows if results[e["fips"]][0].startswith("STALE")]
    print(f"\n{len(stale)} stale, {sum(1 for e in entries if results[e['fips']][0] == 'CURRENT')} verified current, "
          f"{sum(1 for e in entries if results[e['fips']][0] == 'UNVERIFIED')} unverified-fresh, "
          f"{sum(1 for e in entries if results[e['fips']][0] == 'CHECK-FAILED')} check-failed")

    if not args.pull:
        if stale:
            print("re-pull with: python src/refresh.py --pull   (tier with --min-parcels / --limit)")
        return 0

    todo = [e for e in stale if (e.get("feature_count") or 0) >= args.min_parcels]
    if args.limit:
        todo = todo[:args.limit]
    print(f"\nPulling {len(todo)} stale counties (biggest first)...")
    with open(os.path.join(HERE, "county_methods.csv"), encoding="utf-8") as f:
        import csv as _csv
        crosswalk = {r["fips"]: r for r in _csv.DictReader(f)}
    failed = []
    for e in todo:
        print(f"\n== {e['state']} {e['county']} {e['fips']} ({results[e['fips']][1]})")
        try:
            pull_county(e, crosswalk)
        except Exception as ex_:
            failed.append((e["fips"], str(ex_)))
            print(f"  FAIL {e['fips']}: {ex_}")
    print(f"\n{len(todo) - len(failed)} refreshed, {len(failed)} failed.")
    for fips, err in failed:
        print(f"  FAILED {fips}: {err[:160]}")
    if len(todo) > len(failed):
        print("Rebuild the map when convenient: python src/build_map_data.py --state WA")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
