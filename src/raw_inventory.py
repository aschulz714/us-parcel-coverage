"""Raw-artifact inventory: row counts + hashes for every raw county parquet, wherever it lives.

Raw parquets are deleted locally after a verified R2 push (disk), so QA cannot reconcile
raw rows to the source count for most counties by reading a local file. This module keeps
data/parcels/raw_inventory.json = {basename: {rows, bytes, source, r2_etag, sha256,
recorded_at}} built from (a) local raw footers and (b) the parquet FOOTER of each archived
R2 object fetched with a ranged GET (~1 MB, no full download). qa/validate.py consults it
as second-tier evidence after a local raw file and before falling back to normalized rows.

Usage:
  python src/raw_inventory.py --build              # local + R2 (resumable; skips known keys)
  python src/raw_inventory.py --build --refresh    # re-read everything
  python src/raw_inventory.py --record data/parcels/raw/21151_madison.parquet   # after a pull
  python src/raw_inventory.py --show 21151
"""
import argparse
import concurrent.futures as cf
import datetime as _dt
import hashlib
import json
import os
import sys

import pyarrow as pa
import pyarrow.parquet as pq

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
RAW_DIR = os.path.join(ROOT, "data", "parcels", "raw")
INVENTORY = os.path.join(ROOT, "data", "parcels", "raw_inventory.json")
TAIL_BYTES = 1_048_576


def _now():
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load(path=None):
    path = path or INVENTORY
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save(inv, path=None):
    from atomic_io import atomic_write_json
    atomic_write_json(path or INVENTORY, inv, indent=1, sort_keys=True)


def local_entry(path):
    md = pq.read_metadata(path)
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8 * 1024 * 1024), b""):
            h.update(chunk)
    return {"rows": md.num_rows, "bytes": os.path.getsize(path), "source": "local",
            "sha256": h.hexdigest(), "recorded_at": _now()}


def remote_entry(s3, bucket, key):
    """Row count from the archived object's parquet footer via ranged GET; no full download."""
    head = s3.head_object(Bucket=bucket, Key=key)
    size = head["ContentLength"]
    start = max(0, size - TAIL_BYTES)
    tail = s3.get_object(Bucket=bucket, Key=key, Range=f"bytes={start}-")["Body"].read()
    if len(tail) < 12 or tail[-4:] != b"PAR1":
        raise ValueError(f"{key}: not a parquet file (bad magic)")
    flen = int.from_bytes(tail[-8:-4], "little")
    if flen + 8 > len(tail):
        tail = s3.get_object(Bucket=bucket, Key=key, Range=f"bytes={size - flen - 8}-")["Body"].read()
    md = pq.read_metadata(pa.BufferReader(tail))
    return {"rows": md.num_rows, "bytes": size, "source": "r2", "r2_etag": head["ETag"].strip('"'),
            "recorded_at": _now()}


def record(path, inv=None, save_now=True):
    """Record one local raw file (call after every ingest). Returns the entry."""
    inv = load() if inv is None else inv
    entry = local_entry(path)
    inv[os.path.basename(path)] = entry
    if save_now:
        save(inv)
    return entry


def build(refresh=False, workers=8, log=print):
    import r2_sync
    inv = {} if refresh else load()
    local = sorted(f for f in os.listdir(RAW_DIR) if f.endswith(".parquet")) if os.path.isdir(RAW_DIR) else []
    todo_local = [f for f in local if refresh or inv.get(f, {}).get("source") != "local"]
    log(f"local raw: {len(local)} files, {len(todo_local)} to (re)record")
    for i, name in enumerate(todo_local, 1):
        try:
            inv[name] = local_entry(os.path.join(RAW_DIR, name))
        except Exception as exc:
            log(f"  local {name}: {type(exc).__name__}: {exc}")
        if i % 50 == 0:
            save(inv)
    save(inv)
    s3 = r2_sync.client()
    remote = r2_sync.remote_sizes(s3, "raw")
    keys = sorted(k for k in remote if k.endswith(".parquet"))
    todo_remote = [k for k in keys if refresh or os.path.basename(k) not in inv]
    log(f"R2 raw: {len(keys)} objects, {len(todo_remote)} footers to read")
    done = 0
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(remote_entry, s3, r2_sync.BUCKET, k): k for k in todo_remote}
        for fut in cf.as_completed(futs):
            k = futs[fut]
            try:
                inv[os.path.basename(k)] = fut.result()
            except Exception as exc:
                log(f"  r2 {k}: {type(exc).__name__}: {exc}")
            done += 1
            if done % 100 == 0:
                save(inv)
                log(f"  {done}/{len(todo_remote)} remote footers")
    # remember the R2 etag for local entries too, so a later sync check needs no re-hash
    for name, e in inv.items():
        key = f"raw/{name}"
        if key in remote and "r2_etag" not in e:
            e["r2_bytes"] = remote[key]
    save(inv)
    log(f"inventory: {len(inv)} raw artifacts recorded -> {os.path.relpath(INVENTORY, ROOT)}")
    return inv


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--build", action="store_true")
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--record", help="local raw parquet to (re)record")
    ap.add_argument("--show", help="fips prefix to print")
    a = ap.parse_args()
    if a.build:
        build(a.refresh, a.workers)
    elif a.record:
        print(json.dumps(record(a.record), indent=1))
    elif a.show:
        inv = load()
        for k in sorted(inv):
            if k.startswith(a.show):
                print(k, inv[k])
    else:
        ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
