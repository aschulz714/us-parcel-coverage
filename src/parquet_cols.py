"""Column-level parquet rewrite — the fast, geo-safe way to add/replace ATTRIBUTE columns
on a normalized GeoParquet.

Read via pyarrow (geometry stays opaque WKB bytes, never materialized as shapely), swap
columns in place, write back with ALL schema metadata preserved (geoparquet 'geo' + pandas).
~10x faster than the geopandas read/write round-trip and immune to the geo-metadata-loss
class of bug (the DuckDB-COPY trap). Pattern extracted 2026-07-10 from
landuse_std._write_columns / mailing_std so owners_std and value_overlay stop paying the
geopandas tax; landuse_std/mailing_std keep their local copies until they migrate here.
"""
import json

import pyarrow as pa
import pyarrow.parquet as pq

from atomic_io import atomic_replace_writer


def meta_with_cols(meta, cols):
    """Register added STRING cols in the parquet pandas metadata, preserving the geoparquet
    'geo' key. Non-string additions are left unregistered (pandas metadata is advisory;
    readers still load the column with its arrow type)."""
    if not meta or b"pandas" not in meta:
        return meta
    pm = json.loads(meta[b"pandas"])
    have = {c["name"] for c in pm.get("columns", [])}
    for col in cols:
        if col not in have:
            pm["columns"].append({"name": col, "field_name": col, "pandas_type": "unicode",
                                  "numpy_type": "object", "metadata": None})
    out = dict(meta)
    out[b"pandas"] = json.dumps(pm).encode()
    return out


def write_columns(path, table, newcols):
    """Add/replace columns on `table` and write to `path`. newcols = {name: pandas Series}
    (index-aligned to the table's row order). String series are written as pa.string();
    numeric series keep their dtype (nullable Int64 -> int64-with-nulls). Replacements keep
    their original column position; additions append."""
    str_added = []
    for name, ser in newcols.items():
        if ser.dtype == object or str(ser.dtype) in ("string", "string[python]", "string[pyarrow]"):
            arr = pa.array(ser.where(ser.notna(), None).astype(object), type=pa.string())
            str_added.append(name)
        else:
            arr = pa.Array.from_pandas(ser)
        field = pa.field(name, arr.type)
        if name in table.column_names:
            table = table.set_column(table.column_names.index(name), field, arr)
        else:
            table = table.append_column(field, arr)
    table = table.replace_schema_metadata(meta_with_cols(table.schema.metadata, str_added))
    atomic_replace_writer(
        path, lambda temp: pq.write_table(table, temp, compression="zstd"))
    return table
