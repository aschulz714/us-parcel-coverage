"""Resumable, cached-reference-only tract repair. No canonical writes or promotion.

By default OZ values are quarantined as NULL. With --restore-oz, pinned official
original-round polygons determine membership independently of modern tract IDs.
Exact originals remain in each attempt; neither mode promotes data.
"""
import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile

import geopandas as gpd
import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from pyproj import CRS
import shapely

import geo_enrich
import oz_geography

ROOT = Path(__file__).resolve().parents[1]
POLICY = "tract-only-oz-null-v1"
OZ_POLICY = "tract-and-official-2018-oz-v1"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def contained(path, parent):
    path, parent = Path(path).resolve(), Path(parent).resolve()
    require(path != parent and parent in path.parents, f"Unsafe path: {path}")
    return path


def atomic_json(path, value):
    # The caller holds the batch lock. A unique temporary file survives failures.
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                     prefix="report-", suffix=".tmp", delete=False) as out:
        json.dump(value, out, indent=2, sort_keys=True, allow_nan=False)
        out.write("\n")
        out.flush()
        os.fsync(out.fileno())
        temp = out.name
    os.replace(temp, path)


@contextmanager
def batch_lock(directory):
    """OS-released lock: interruption does not require deleting a stale lock."""
    with (directory / "run.lock").open("a+b") as handle:
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def check_input(table):
    require(len(set(table.column_names)) == len(table.column_names), "Duplicate column names")
    for name in ("geometry", "census_tract"):
        require(name in table.column_names, f"Missing required column: {name}")
    require(pa.types.is_string(table.schema.field("census_tract").type),
            "Expected existing string census_tract")
    meta = json.loads((table.schema.metadata or {}).get(b"geo", b"{}"))
    require(meta.get("primary_column") == "geometry", "Unknown primary geometry")
    geometry = meta.get("columns", {}).get("geometry", {})
    require(geometry.get("encoding") == "WKB", "Expected WKB geometry")
    # Absent CRS in GeoParquet means CRS84; explicit null is unknown and rejected.
    crs = geometry.get("crs", "OGC:CRS84")
    require(crs is not None and CRS.from_user_input(crs).equals(CRS(4326), ignore_axis_order=True),
            "Parcel CRS must be known WGS84 longitude/latitude")
    for name in ("opportunity_zone", "opportunity_zone_2018"):
        if name in table.column_names:
            field = table.schema.field(name)
            require(pa.types.is_boolean(field.type) and field.nullable,
                    f"{name} must be nullable boolean")


def check_tracts(tracts, state=None):
    require(tracts.crs is not None and CRS(tracts.crs).equals(CRS(4326), ignore_axis_order=True),
            "Tracts must be WGS84")
    require(len(tracts) > 0 and "GEOID" in tracts, "Empty/missing tract IDs")
    ids = tracts["GEOID"]
    require(ids.notna().all() and ids.is_unique and
            ids.astype(str).str.fullmatch(r"\d{11}").all(), "Invalid/duplicate tract IDs")
    if state:
        require(ids.str.startswith(state).all(), "Wrong-state tract reference")
    require(tracts.geometry.is_valid.all() and not tracts.geometry.is_empty.any() and
            tracts.geometry.geom_type.isin(["Polygon", "MultiPolygon"]).all(),
            "Invalid tract polygons")


def validate_spatial(table, tracts):
    """Independent STRtree check, including missed matches (not only assigned rows)."""
    geoms = shapely.from_wkb(table["geometry"].to_numpy())
    valid = shapely.is_valid(geoms) & ~shapely.is_missing(geoms) & ~shapely.is_empty(geoms)
    points = shapely.point_on_surface(geoms[valid])
    pairs = shapely.STRtree(tracts.geometry.to_numpy()).query(points, predicate="within")
    matched = np.zeros(len(points), dtype=bool)
    matched[pairs[0]] = True
    assigned = table["census_tract"].to_pandas().astype("string")
    require(not assigned[~valid].notna().any(), "Invalid/missing/empty geometry assigned a tract")
    actual = assigned[valid].reset_index(drop=True)
    require(np.array_equal(actual.notna().to_numpy(), matched), "Missed or spurious tract assignment")
    if matched.any():
        lookup = tracts.set_index("GEOID").geometry
        require(actual[matched].isin(lookup.index).all(), "Unknown assigned tract ID")
        shapes = lookup.loc[actual[matched].tolist()].to_numpy()
        require(shapely.within(points[matched], shapes).all(), "Parcel assigned to wrong tract")
    counts = np.bincount(pairs[0], minlength=len(points))
    return {"assigned_spatially_verified": int(matched.sum()),
            "invalid_missing_or_empty": int((~valid).sum()),
            "valid_without_within_match": int((~matched).sum()),
            "multiple_matches": int((counts > 1).sum())}


def repair_table(table, tracts, oz_boundaries=None):
    check_input(table)
    check_tracts(tracts)
    # Do not compute occupancy or consult the OZ designation list.
    series = geo_enrich.enrich_table(table.select(["geometry"]), tracts, set())["census_tract"]
    field = table.schema.field("census_tract")
    result = table.set_column(table.column_names.index("census_tract"), field,
                              pa.array(series, type=field.type, from_pandas=True))
    flags = (pa.Array.from_pandas(oz_geography.classify(
        table, oz_boundaries, series.notna().to_numpy())) if oz_boundaries is not None
        else pa.nulls(table.num_rows, type=pa.bool_()))
    if "opportunity_zone" in table.column_names:
        field = table.schema.field("opportunity_zone")
        result = result.set_column(table.column_names.index("opportunity_zone"), field,
                                   flags)
    if oz_boundaries is not None:
        field = oz_geography.versioned_field()
        if field.name in result.column_names:
            result = result.set_column(result.column_names.index(field.name), field, flags)
        else:
            result = result.append_column(field, flags)
    elif "opportunity_zone_2018" in result.column_names:
        field = result.schema.field("opportunity_zone_2018")
        result = result.set_column(result.column_names.index(field.name), field,
                                   pa.nulls(result.num_rows, type=field.type))
    return result


def validate_rewrite(original, rebuilt, tracts, oz_boundaries=None):
    require(original.num_rows == rebuilt.num_rows, "Row count changed")
    expected_schema = original.schema
    if oz_boundaries is not None:
        field = oz_geography.versioned_field()
        index = expected_schema.get_field_index(field.name)
        expected_schema = expected_schema.set(index, field) if index >= 0 else expected_schema.append(field)
    require(expected_schema.equals(rebuilt.schema, check_metadata=True), "Unexpected schema/metadata change")
    for name in original.column_names:
        if name not in ("census_tract", "opportunity_zone", "opportunity_zone_2018"):
            require(original[name].equals(rebuilt[name]), f"Unrelated column changed: {name}")
    stats = validate_spatial(rebuilt, tracts)
    if oz_boundaries is not None:
        if "opportunity_zone" in rebuilt.column_names:
            require(rebuilt["opportunity_zone"].equals(rebuilt["opportunity_zone_2018"]), "OZ alias differs")
        stats.update(oz_geography.validate_flags(
            rebuilt, oz_boundaries, rebuilt["census_tract"].to_pandas().notna().to_numpy(),
            rebuilt["opportunity_zone_2018"].to_pandas()))
    else:
        for name in ("opportunity_zone", "opportunity_zone_2018"):
            if name in rebuilt.column_names:
                require(rebuilt[name].null_count == rebuilt.num_rows, "OZ not quarantined")
    return stats


def load_cached_tracts(path, state):
    # No fallback download, CRS guess, or calls to geo_enrich.ensure_tiger.
    tracts = gpd.read_file(f"zip://{path}")
    require(tracts.crs is not None, "Cached reference CRS is unknown")
    tracts = tracts[["GEOID", "geometry"]].to_crs(4326)
    check_tracts(tracts, state)
    return tracts


def fingerprint(source, reference, oz_reference=None):
    result = {"policy": OZ_POLICY if oz_reference else POLICY, "source": str(source), "source_sha256": digest(source),
            "reference": str(reference), "reference_sha256": digest(reference),
            "repair_code_sha256": digest(Path(__file__)),
            "enrichment_code_sha256": digest(Path(geo_enrich.__file__)),
            "libraries": {"geopandas": gpd.__version__, "shapely": shapely.__version__,
                          "pyarrow": pa.__version__, "pandas": pd.__version__}}
    result["oz_code_sha256"] = digest(Path(oz_geography.__file__))
    if oz_reference:
        result.update({"oz_reference": str(oz_reference), "oz_reference_sha256": digest(oz_reference)})
    return result


def resumable_report(directory, identity):
    checkpoint = directory / "complete.json"
    if not checkpoint.exists():
        return None
    try:
        report = json.loads(checkpoint.read_text(encoding="utf-8"))
        if report["identity"] != identity or report["status"] != "validated_staging_only":
            return None
        for name in ("backup", "staged"):
            artifact = contained(directory / report[name], directory)
            if not artifact.is_file() or digest(artifact) != report[name + "_sha256"]:
                return None
        if report["backup_sha256"] != identity["source_sha256"]:
            return None
        validation = contained(directory / report["staged"], directory).parent / "validation.json"
        if json.loads(validation.read_text(encoding="utf-8")) != report:
            return None
        return report
    except (ValueError, KeyError, OSError, TypeError):
        return None


def stage_county(source, directory, reference, identity, tracts, oz_boundaries=None):
    require(shutil.disk_usage(directory).free > source.stat().st_size * 3,
            "Insufficient staging disk headroom (requires 3x source size)")
    attempt = Path(tempfile.mkdtemp(prefix="attempt-", dir=directory))
    backup, staged = attempt / "original.parquet", attempt / "repaired.parquet"
    shutil.copy2(source, backup)
    require(digest(backup) == identity["source_sha256"], "Source changed while backing up")
    original = pq.read_table(backup)
    rebuilt = repair_table(original, tracts, oz_boundaries)
    pq.write_table(rebuilt, staged, compression="zstd")
    stats = validate_rewrite(original, pq.read_table(staged), tracts, oz_boundaries)
    require(digest(source) == identity["source_sha256"], "Canonical source changed during repair")
    require(digest(reference) == identity["reference_sha256"], "Reference changed during repair")
    if oz_boundaries is not None:
        require(digest(identity["oz_reference"]) == identity["oz_reference_sha256"], "OZ reference changed during repair")
    old = original["census_tract"].to_pandas().astype("string")
    new = rebuilt["census_tract"].to_pandas().astype("string")
    changed = ~((old.isna() & new.isna()) | old.eq(new).fillna(False))
    report = {"status": "validated_staging_only", "identity": identity,
              "rows": original.num_rows, "changed_tract_rows": int(changed.sum()),
              "opportunity_zone_policy": (oz_geography.METHOD if oz_boundaries is not None else
                                          "all NULL in staging; designation accuracy unresolved"),
              "oz_values_quarantined": (original.num_rows - original["opportunity_zone"].null_count
                                        if "opportunity_zone" in original.column_names else 0),
              "unrelated_columns_and_metadata_preserved": True,
              "versioned_oz_field_with_provenance": oz_boundaries is not None,
              "canonical_source_unchanged": True, "promotion_allowed": False,
              "backup": backup.relative_to(directory).as_posix(), "backup_sha256": digest(backup),
              "staged": staged.relative_to(directory).as_posix(), "staged_sha256": digest(staged),
              **stats}
    if oz_boundaries is not None:
        report.pop("oz_values_quarantined")
        report["oz_reference_quarantined_geoids"] = oz_boundaries.loc[
            oz_boundaries.get("quarantined", pd.Series(False, index=oz_boundaries.index)), "GEOID"].tolist()
        if "opportunity_zone" in original.column_names:
            old_oz = original["opportunity_zone"].to_pandas().astype("boolean")
            new_oz = rebuilt["opportunity_zone"].to_pandas().astype("boolean")
            report["changed_oz_rows"] = int((~((old_oz.isna() & new_oz.isna()) |
                                              old_oz.eq(new_oz).fillna(False))).sum())
    atomic_json(attempt / "validation.json", report)
    # Only a fully written and read-back validated artifact gets a completion marker.
    atomic_json(directory / "complete.json", report)
    return report


def run_batch(root, entries, batch="pilot", max_counties=5, restore_oz=False):
    root = Path(root).resolve()
    require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", batch), "Invalid batch name")
    require(max_counties > 0, "max_counties must be positive")
    directory = contained(root / ".tmp" / "tract-repair" / batch, root)
    normalized = contained(root / "data/parcels/normalized", root)
    require(not (directory == normalized or normalized in directory.parents or directory in normalized.parents),
            "Staging overlaps canonical data")
    directory.mkdir(parents=True, exist_ok=True)
    for fips in entries:
        require(re.fullmatch(r"\d{5}", fips), f"Invalid FIPS: {fips}")
    summary = {"status": "in_progress", "policy": OZ_POLICY if restore_oz else POLICY, "promotion_allowed": False,
               "selected": len(entries), "validated": 0, "resumed": 0, "failed": 0,
               "pending": len(entries), "counties": {fips: {"status": "pending"} for fips in entries}}
    attempts = 0
    cached_key, cached_tracts = None, None
    oz_reference = contained(oz_geography.cache_path(root), root) if restore_oz else None
    oz_boundaries = None
    loaded_oz_hash = None
    with batch_lock(directory):
        atomic_json(directory / "summary.json", summary)
        for fips, entry in sorted(entries.items()):
            try:
                source = contained(root / entry["normalized_file"], normalized)
                require(source.name.startswith(fips + "_"), "Source filename/FIPS mismatch")
                reference = contained(root / f"data/reference/tiger/tl_2023_{fips[:2]}_tract.zip", root)
                identity = fingerprint(source, reference, oz_reference)
                county_dir = contained(directory / fips, directory)
                county_dir.mkdir(exist_ok=True)
                report = resumable_report(county_dir, identity)
                if report is not None:
                    if restore_oz:
                        require(digest(oz_reference) == identity["oz_reference_sha256"], "OZ reference changed during resume")
                    require(digest(source) == identity["source_sha256"] and
                            digest(reference) == identity["reference_sha256"],
                            "Source/reference changed during resume verification")
                    summary["resumed"] += 1
                    action = "resumed"
                elif attempts >= max_counties:
                    summary["counties"][fips] = {"status": "pending"}
                    continue
                else:
                    attempts += 1
                    key = (str(reference), identity["reference_sha256"])
                    if key != cached_key:
                        cached_tracts = load_cached_tracts(reference, fips[:2])
                        require(digest(reference) == identity["reference_sha256"], "Reference changed while loading")
                        cached_key = key
                    if restore_oz and oz_boundaries is None:
                        oz_boundaries = oz_geography.load_zones(root)
                        loaded_oz_hash = identity["oz_reference_sha256"]
                    if restore_oz:
                        require(identity["oz_reference_sha256"] == loaded_oz_hash,
                                "OZ reference changed between counties; cached geometry is stale")
                    report = stage_county(source, county_dir, reference, identity, cached_tracts, oz_boundaries)
                    action = "validated"
                summary["validated"] += 1
                summary["pending"] -= 1
                summary["counties"][fips] = {"status": action, "rows": report["rows"],
                                            "changed_tract_rows": report["changed_tract_rows"],
                                            "report": f"{fips}/complete.json"}
                print(f"{fips}: {action}; {report['rows']:,} rows; "
                      f"{report['changed_tract_rows']:,} tract changes", flush=True)
            except Exception as exc:
                summary["failed"] += 1
                summary["pending"] -= 1
                summary["counties"][fips] = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
                print(f"{fips}: FAILED: {exc}", flush=True)
            atomic_json(directory / "summary.json", summary)
        summary["status"] = ("failed" if summary["failed"] else "partial" if summary["pending"]
                             else "validated_staging_only")
        atomic_json(directory / "summary.json", summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--fips", nargs="+")
    selection.add_argument("--state")
    parser.add_argument("--batch", default="pilot")
    parser.add_argument("--restore-oz", action="store_true",
                        help="restore original-round OZ flags from pinned official boundaries in staging")
    parser.add_argument("--max-counties", type=int, default=5,
                        help="maximum new county repair attempts per invocation (default 5)")
    args = parser.parse_args()
    manifest = json.loads((ROOT / "data/parcels/manifest.json").read_text(encoding="utf-8"))
    if args.fips:
        if any(fips not in manifest for fips in args.fips):
            parser.error("Unknown FIPS in selection")
        entries = {fips: manifest[fips] for fips in args.fips}
    else:
        entries = {fips: entry for fips, entry in manifest.items()
                   if entry.get("state") == args.state.upper() and entry.get("normalized_file")}
    if not entries:
        parser.error("No counties selected")
    result = run_batch(ROOT, entries, args.batch, args.max_counties, args.restore_oz)
    print(json.dumps({k: v for k, v in result.items() if k != "counties"}, indent=2))
    return 1 if result["failed"] else 2 if result["pending"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
