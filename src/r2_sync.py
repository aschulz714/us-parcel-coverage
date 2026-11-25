"""Cloudflare R2 archive sync — cold storage for raw parquets, source archives, tiles.

R2 is S3-compatible with ZERO egress fees, which makes it the project's cold tier
(~$0.015/GB/mo) and, later, the public PMTiles host. Credentials live in
~/.r2.env (R2_ENDPOINT / R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY) — NEVER in the
repo; .gitignore guards *.r2.env*.

Layout in the parcel-archive bucket mirrors the local tree:
  bulk_src/...   raw/...   tiles/...

Usage:
  python src/r2_sync.py test                      # round-trip self-test
  python src/r2_sync.py push data/parcels/bulk_src --prefix bulk_src
  python src/r2_sync.py push data/parcels/raw --prefix raw
  python src/r2_sync.py pull raw/12086_miami_dade.parquet data/parcels/raw/
  python src/r2_sync.py ls [prefix]

push skips files whose size already matches remotely (cheap resume); deletion is
NEVER automatic — freeing local disk after a verified push is a manual decision.
"""
import argparse, os, sys

import boto3
from botocore.config import Config

BUCKET = "parcel-archive"


def client():
    env = {}
    with open(os.path.expanduser("~/.r2.env"), encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if line and "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return boto3.client(
        "s3", endpoint_url=env["R2_ENDPOINT"],
        aws_access_key_id=env["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=env["R2_SECRET_ACCESS_KEY"],
        config=Config(retries={"max_attempts": 5, "mode": "adaptive"}),
        region_name="auto")


def remote_sizes(s3, prefix):
    sizes = {}
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=BUCKET, Prefix=prefix):
        for o in page.get("Contents", []):
            sizes[o["Key"]] = o["Size"]
    return sizes


def _remote_etags(s3, prefix):
    et = {}
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=BUCKET, Prefix=prefix):
        for o in page.get("Contents", []):
            et[o["Key"]] = o["ETag"].strip('"')
    return et


def push(path, prefix, check_hash=False):
    """Skip logic: by default a remote file with the same SIZE is assumed current (cheap
    resume). --check-hash compares the CONTENT ETag instead — required after an in-place
    rewrite where a changed file can keep its old size (mailing_norm v2 rewrote all 2,298
    normalized files; a size-only skip would leave stale content on R2). ETag skip also
    gives exact resume: a re-run after an interrupted push re-reads locally but re-uploads
    only what is genuinely not-yet-current."""
    s3 = client()
    have = _remote_etags(s3, prefix) if check_hash else remote_sizes(s3, prefix)
    files = []
    if os.path.isfile(path):
        files = [(path, f"{prefix}/{os.path.basename(path)}" if prefix else os.path.basename(path))]
    else:
        for root, _, names in os.walk(path):
            for n in names:
                p = os.path.join(root, n)
                rel = os.path.relpath(p, path).replace("\\", "/")
                files.append((p, f"{prefix}/{rel}" if prefix else rel))
    sent = skipped = 0
    total = sum(os.path.getsize(p) for p, _ in files)
    done = 0
    for p, key in sorted(files):
        size = os.path.getsize(p)
        done += size
        current = (have.get(key) == _expected_etag(p)) if check_hash else (have.get(key) == size)
        if current:
            skipped += 1
            continue
        s3.upload_file(p, BUCKET, key)
        sent += 1
        if sent % 25 == 0 or size > 50e6:
            print(f"  up {key} ({size/1e6:,.1f} MB)  [{done/total:.0%} of {total/1e9:,.2f} GB, "
                  f"{sent} sent]", flush=True)
    print(f"push done: {sent} uploaded, {skipped} already current", flush=True)


def pull(key, dest_dir):
    s3 = client()
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, os.path.basename(key))
    s3.download_file(BUCKET, key, dest)
    print(f"pulled {key} -> {dest} ({os.path.getsize(dest)/1e6:,.1f} MB)")


def ls(prefix):
    s3 = client()
    sizes = remote_sizes(s3, prefix or "")
    for k in sorted(sizes):
        print(f"  {sizes[k]/1e6:10,.1f} MB  {k}")
    print(f"{len(sizes)} objects, {sum(sizes.values())/1e9:,.2f} GB total")


def _expected_etag(path, chunk=8 * 1024 * 1024):
    """The ETag R2 stores: md5 of the content for single-part uploads; for multipart
    (boto3 default threshold/chunk = 8MB), md5-of-part-md5s + '-<nparts>'."""
    import hashlib
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        if size <= chunk:
            return hashlib.md5(f.read()).hexdigest()
        digests = []
        while True:
            buf = f.read(chunk)
            if not buf:
                break
            digests.append(hashlib.md5(buf).digest())
        return hashlib.md5(b"".join(digests)).hexdigest() + f"-{len(digests)}"


def verify(path, prefix):
    """Content-hash verification of an entire pushed tree against R2 — no downloads."""
    s3 = client()
    remote = {}
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=BUCKET, Prefix=prefix):
        for o in page.get("Contents", []):
            remote[o["Key"]] = o["ETag"].strip('"')
    ok = bad = missing = 0
    for root, _, names in os.walk(path):
        for n in names:
            p = os.path.join(root, n)
            rel = os.path.relpath(p, path).replace("\\", "/")
            key = f"{prefix}/{rel}" if prefix else rel
            et = remote.get(key)
            if et is None:
                missing += 1
                print(f"  MISSING remote: {key}", flush=True)
            elif et == _expected_etag(p):
                ok += 1
            else:
                bad += 1
                print(f"  HASH MISMATCH: {key}", flush=True)
    print(f"verify: {ok} content-verified, {bad} mismatched, {missing} missing", flush=True)
    sys.exit(1 if (bad or missing) else 0)


def test():
    import hashlib, tempfile
    s3 = client()
    payload = b"national-parcel-coverage r2 round-trip test\n" * 100
    key = "_test/roundtrip.bin"
    with tempfile.NamedTemporaryFile(delete=False, suffix=".bin") as f:
        f.write(payload)
        tmp = f.name
    s3.upload_file(tmp, BUCKET, key)
    got = s3.get_object(Bucket=BUCKET, Key=key)["Body"].read()
    assert hashlib.sha256(got).hexdigest() == hashlib.sha256(payload).hexdigest(), "payload mismatch"
    s3.delete_object(Bucket=BUCKET, Key=key)
    os.unlink(tmp)
    print("R2 round-trip OK: upload, download, hash-verify, delete all succeeded")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("test")
    p = sub.add_parser("push"); p.add_argument("path"); p.add_argument("--prefix", default="")
    p.add_argument("--check-hash", action="store_true",
                   help="skip by content ETag not size (use after an in-place rewrite)")
    p = sub.add_parser("verify"); p.add_argument("path"); p.add_argument("--prefix", default="")
    p = sub.add_parser("pull"); p.add_argument("key"); p.add_argument("dest")
    p = sub.add_parser("ls"); p.add_argument("prefix", nargs="?", default="")
    a = ap.parse_args()
    if a.cmd == "test":
        test()
    elif a.cmd == "push":
        push(a.path, a.prefix.rstrip("/"), check_hash=a.check_hash)
    elif a.cmd == "verify":
        verify(a.path, a.prefix.rstrip("/"))
    elif a.cmd == "pull":
        pull(a.key, a.dest)
    elif a.cmd == "ls":
        ls(a.prefix)


if __name__ == "__main__":
    main()
