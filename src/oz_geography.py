"""Original 2018-round opportunity zones, using CDFI's designated polygons.

This is a representative-point geographic indicator, NOT a tax-eligibility or
whole-parcel containment determination. Modern tract IDs never determine membership.
"""
import hashlib
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import pyarrow as pa
from pyproj import CRS
import shapely

SOURCE_URL = "https://www.cdfifund.gov/system/files/documents/opportunity-zones%3D8764.-9-10-2019.zip"
# Filled only after verifying the official downloaded archive and its contents.
SOURCE_SHA256 = "686256f36e2f2f5da6dcea9d6a3f6424eeb994fb97f4f725da14c31576ef7af9"
CACHE_NAME = "opportunity_zones_2018_cdfi_20190910.zip"
METHOD = "representative_point_within_official_2018_zone; boundary_or_ambiguous_unknown"


def file_hash(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def cache_path(root):
    return Path(root) / "data/reference" / CACHE_NAME


def validate_zones(zones):
    if zones.crs is None or not CRS(zones.crs).equals(CRS(4326), ignore_axis_order=True):
        raise ValueError("OZ reference must be WGS84")
    if "GEOID" not in zones or len(zones) == 0:
        raise ValueError("Missing OZ polygons/IDs")
    ids = zones["GEOID"]
    if not (ids.notna().all() and ids.is_unique and ids.str.fullmatch(r"\d{11}").all()):
        raise ValueError("Invalid/duplicate OZ IDs")
    if not (zones.geometry.is_valid.all() and not zones.geometry.is_empty.any() and
            zones.geometry.geom_type.isin(["Polygon", "MultiPolygon"]).all()):
        raise ValueError("Invalid OZ geometry; do not silently repair official boundaries")


def load_zones(root):
    """Load a pinned official cache; never download or infer a missing reference."""
    path = cache_path(root)
    if file_hash(path) != SOURCE_SHA256:
        raise ValueError("OZ reference checksum mismatch; expected the verified CDFI archive")
    zones = gpd.read_file(f"zip://{path}")
    if zones.crs is None:
        raise ValueError("OZ reference has unknown CRS")
    zones = zones.rename(columns={"CENSUSTRAC": "GEOID"})[["GEOID", "geometry"]].to_crs(4326)
    # The pinned archive contains one self-intersecting Madera CA polygon.
    # Its envelope is an uncertainty mask, NEVER a replacement designated zone.
    bad = ~zones.geometry.is_valid
    if set(zones.loc[bad, "GEOID"]) != {"06039000202"}:
        raise ValueError("Unexpected invalid OZ geometry inventory")
    zones["quarantined"] = bad
    zones.loc[bad, "geometry"] = zones.loc[bad, "geometry"].envelope
    validate_zones(zones)
    if len(zones) != 8764:
        raise ValueError("Incomplete official OZ reference: expected 8,764 designated zones")
    if file_hash(path) != SOURCE_SHA256:
        raise ValueError("OZ reference changed during load")
    return zones


def versioned_field():
    return pa.field("opportunity_zone_2018", pa.bool_(), metadata={
        b"designation_round": b"2018", b"source_url": SOURCE_URL.encode(),
        b"source_sha256": SOURCE_SHA256.encode(), b"method": METHOD.encode()})


def classify(table, zones, covered):
    """False only for covered, valid points outside zones; missing coverage -> unknown.

`covered` indicates an independent land/tract coverage match, not OZ membership.
Inside an official zone can be True even without a modern-tract coverage match.
"""
    n = table.num_rows
    result = pd.Series(pd.array([None] * n, dtype="boolean"))
    if zones is None:
        return result  # No fallback to modern GEOID membership.
    validate_zones(zones)
    if "geometry" not in table.column_names or not n:
        return result
    if len(covered) != n:
        raise ValueError("OZ coverage mask length mismatch")
    geoms = shapely.from_wkb(table["geometry"].to_numpy())
    valid = shapely.is_valid(geoms) & ~shapely.is_missing(geoms) & ~shapely.is_empty(geoms)
    rows = np.flatnonzero(valid)
    if not len(rows):
        return result
    points = gpd.GeoDataFrame(geometry=gpd.GeoSeries(
        shapely.point_on_surface(geoms[valid]), index=rows, crs=4326))
    usable = zones.loc[~zones["quarantined"]] if "quarantined" in zones else zones
    inside = gpd.sjoin(points, usable[["GEOID", "geometry"]], how="inner", predicate="within")
    intersects = gpd.sjoin(points, zones[["GEOID", "geometry"]], how="inner", predicate="intersects")
    coverage = np.asarray(covered, dtype=bool)
    result.loc[rows[coverage[rows]]] = False
    # Any boundary contact or multiple polygon matches remains unknown.
    result.loc[intersects.index.unique()] = pd.NA
    single = intersects.groupby(level=0).size()
    interior_rows = inside.index.unique().intersection(single[single == 1].index)
    result.loc[interior_rows] = True
    return result


def validate_flags(table, zones, covered, actual):
    """Independent STRtree validation of both positive and negative/unknown flags."""
    geoms = shapely.from_wkb(table["geometry"].to_numpy())
    valid = shapely.is_valid(geoms) & ~shapely.is_missing(geoms) & ~shapely.is_empty(geoms)
    rows = np.flatnonzero(valid)
    points = shapely.point_on_surface(geoms[valid])
    tree = shapely.STRtree(zones.geometry.to_numpy())
    usable = zones.loc[~zones["quarantined"]] if "quarantined" in zones else zones
    inside = shapely.STRtree(usable.geometry.to_numpy()).query(points, predicate="within")
    hits = tree.query(points, predicate="intersects")
    hit_count = np.bincount(hits[0], minlength=len(rows))
    inside_count = np.bincount(inside[0], minlength=len(rows))
    expected = pd.Series(pd.array([None] * table.num_rows, dtype="boolean"))
    expected.loc[rows[np.asarray(covered, dtype=bool)[rows] & (hit_count == 0)]] = False
    expected.loc[rows[(inside_count == 1) & (hit_count == 1)]] = True
    actual = pd.Series(actual, dtype="boolean").reset_index(drop=True)
    pd.testing.assert_series_equal(actual, expected, check_names=False)
    return {"oz_true": int(actual.fillna(False).sum()),
            "oz_false": int((~actual).fillna(False).sum()),
            "oz_unknown": int(actual.isna().sum()),
            "oz_boundary_or_ambiguous": int(((hit_count > 0) & ~((inside_count == 1) & (hit_count == 1))).sum())}
