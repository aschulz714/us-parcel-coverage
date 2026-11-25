"""Promote validated staged tract/OZ repairs into canonical normalized parcel files.

This is the ONLY step that writes canonical data in the tract-repair lane. It never
computes geography itself; it copies a staged artifact that stage_tract_repair.py
already validated, and only when every piece of evidence still lines up:

  * the county's complete.json has status "validated_staging_only";
  * the canonical file's current SHA-256 equals the identity source hash recorded at
    staging time AND the exact backup hash (nothing touched the file since staging);
  * the staged file exists and its SHA-256 equals the recorded staged_sha256;
  * the staged parquet footer row count equals the recorded row count.

Replacement is a same-volume temp copy + os.replace (atomic on NTFS), followed by a
read-back SHA-256 of the canonical file that must equal staged_sha256. Every promotion
appends one JSON line to data/parcels/tract_repair_ledger.jsonl (before/after hashes,
row deltas, policy) and writes <batch>/<fips>/promoted.json. Re-running is idempotent:
an already-promoted county is recognised by hash, and a crash between replace and marker
is recovered on the next run.

--reclaim deletes original.parquet / repaired.parquet payloads inside the county's
attempt directories after a hash-verified promotion. The JSON evidence stays. Only use
this when the pre-repair originals are known to exist elsewhere (R2 normalized/ tree
verified before the batch started); the driver records that check.

Usage:
  python src/promote_tract_repair.py --batch oz-2018-boundaries-20260907 --dry-run
  python src/promote_tract_repair.py --batch natl-10 --reclaim
  python src/promote_tract_repair.py --batch natl-10 --fips 10001 10003
"""
import argparse
import datetime as _dt
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

import pyarrow.parquet as pq

from atomic_io import atomic_replace_writer

ROOT = Path(__file__).resolve().parents[1]
LEDGER_REL = Path("data/parcels/tract_repair_ledger.jsonl")
PAYLOADS = ("original.parquet", "repaired.parquet")


class PromotionRefused(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise PromotionRefused(message)


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


def _now():
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def _write_json(path, value):
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as out:
        json.dump(value, out, indent=2, sort_keys=True)
        out.write("\n")
        out.flush()
        os.fsync(out.fileno())
    os.replace(tmp, path)


def _append_ledger(ledger, record):
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with ledger.open("a", encoding="utf-8") as out:
        out.write(json.dumps(record, sort_keys=True) + "\n")
        out.flush()
        os.fsync(out.fileno())


def load_report(county_dir):
    checkpoint = county_dir / "complete.json"
    require(checkpoint.is_file(), "No complete.json (county never validated in staging)")
    report = json.loads(checkpoint.read_text(encoding="utf-8"))
    require(report.get("status") == "validated_staging_only", f"Unexpected staging status: {report.get('status')}")
    identity = report["identity"]
    for key in ("source", "source_sha256"):
        require(key in identity, f"Staging identity lacks {key}")
    require(report["backup_sha256"] == identity["source_sha256"], "Backup hash != staged identity source hash")
    return report


def reclaim_payloads(county_dir):
    """Delete parquet payloads from every attempt dir; keep JSON evidence. Returns bytes freed."""
    freed = 0
    for attempt in county_dir.glob("attempt-*"):
        for name in PAYLOADS:
            p = attempt / name
            if p.is_file():
                freed += p.stat().st_size
                p.unlink()
    return freed


def promote_county(root, county_dir, normalized, ledger, batch, reclaim=False, dry_run=False):
    """Returns (status, detail). status in {promoted, recovered, already_promoted, dry_run_ok}.
    Raises PromotionRefused on any evidence mismatch; nothing is written in that case."""
    fips = county_dir.name
    report = load_report(county_dir)
    identity = report["identity"]
    canonical = contained(root / identity["source"] if not Path(identity["source"]).is_absolute()
                          else identity["source"], normalized)
    require(canonical.name.startswith(fips + "_"), "Canonical filename/FIPS mismatch")
    staged = contained(county_dir / report["staged"], county_dir)
    marker = county_dir / "promoted.json"

    current = digest(canonical)
    if current == report["staged_sha256"]:
        if marker.is_file():
            record = json.loads(marker.read_text(encoding="utf-8"))
            if reclaim and not dry_run and any((a / n).is_file() for a in county_dir.glob("attempt-*") for n in PAYLOADS):
                # Promoted earlier without --reclaim: a later reclaim pass may free the payloads.
                record["reclaimed_bytes"] = record.get("reclaimed_bytes", 0) + reclaim_payloads(county_dir)
                _write_json(marker, record)
            return "already_promoted", record
        # Crash window between os.replace and marker write: finish the bookkeeping.
        record = _record(fips, batch, report, canonical, current, recovered=True)
        if not dry_run:
            _append_ledger(ledger, record)
            _write_json(marker, record)
            if reclaim:
                record["reclaimed_bytes"] = reclaim_payloads(county_dir)
                _write_json(marker, record)
        return "recovered", record

    require(current == identity["source_sha256"],
            f"Canonical file changed since staging (now {current[:12]}..., staged from "
            f"{identity['source_sha256'][:12]}...); re-stage this county")
    require(staged.is_file(), "Staged artifact missing")
    require(digest(staged) == report["staged_sha256"], "Staged artifact hash mismatch")
    footer = pq.read_metadata(staged)
    require(footer.num_rows == report["rows"], f"Staged footer rows {footer.num_rows} != report rows {report['rows']}")
    require(shutil.disk_usage(canonical.parent).free > staged.stat().st_size * 2,
            "Insufficient free disk on canonical volume for atomic replace")

    if dry_run:
        return "dry_run_ok", {"fips": fips, "rows": report["rows"],
                              "changed_tract_rows": report["changed_tract_rows"],
                              "changed_oz_rows": report.get("changed_oz_rows")}

    def _copy(temp_path):
        shutil.copyfile(staged, temp_path)
        with open(temp_path, "rb+") as handle:
            os.fsync(handle.fileno())
        require(digest(temp_path) == report["staged_sha256"], "Temp copy hash mismatch before replace")

    atomic_replace_writer(str(canonical), _copy)
    after = digest(canonical)
    require(after == report["staged_sha256"], "Canonical hash after replace != staged hash (INVESTIGATE)")

    record = _record(fips, batch, report, canonical, after, recovered=False)
    _append_ledger(ledger, record)
    _write_json(marker, record)
    if reclaim:
        record["reclaimed_bytes"] = reclaim_payloads(county_dir)
        _write_json(marker, record)
    return "promoted", record


def _record(fips, batch, report, canonical, after_sha, recovered):
    identity = report["identity"]
    return {"fips": fips, "batch": batch, "promoted_at": _now(), "recovered_marker": recovered,
            "canonical": canonical.relative_to(ROOT).as_posix() if ROOT in canonical.parents else str(canonical),
            "before_sha256": identity["source_sha256"], "after_sha256": after_sha,
            "rows": report["rows"], "changed_tract_rows": report["changed_tract_rows"],
            "changed_oz_rows": report.get("changed_oz_rows"),
            "policy": identity.get("policy"), "oz_reference_sha256": identity.get("oz_reference_sha256"),
            "reference_sha256": identity.get("reference_sha256"),
            "repair_code_sha256": identity.get("repair_code_sha256"),
            "enrichment_code_sha256": identity.get("enrichment_code_sha256"),
            "oz_code_sha256": identity.get("oz_code_sha256"),
            "assigned_spatially_verified": report.get("assigned_spatially_verified"),
            "invalid_missing_or_empty": report.get("invalid_missing_or_empty")}


def promote_batch(root, batch, fips=None, reclaim=False, dry_run=False, log=print):
    root = Path(root).resolve()
    directory = contained(root / ".tmp" / "tract-repair" / batch, root)
    require(directory.is_dir(), f"Batch directory missing: {directory}")
    normalized = contained(root / "data/parcels/normalized", root)
    ledger = root / LEDGER_REL
    county_dirs = sorted(p for p in directory.iterdir() if p.is_dir() and p.name.isdigit() and len(p.name) == 5)
    if fips:
        wanted = set(fips)
        county_dirs = [p for p in county_dirs if p.name in wanted]
        missing = wanted - {p.name for p in county_dirs}
        require(not missing, f"No staging directory for {sorted(missing)}")
    summary = {"batch": batch, "dry_run": dry_run, "reclaim": reclaim, "selected": len(county_dirs),
               "promoted": 0, "recovered": 0, "already_promoted": 0, "dry_run_ok": 0,
               "refused": 0, "counties": {}}
    for county_dir in county_dirs:
        code = county_dir.name
        try:
            status, detail = promote_county(root, county_dir, normalized, ledger, batch, reclaim, dry_run)
            summary[status] += 1
            summary["counties"][code] = {"status": status,
                                         "changed_tract_rows": detail.get("changed_tract_rows"),
                                         "changed_oz_rows": detail.get("changed_oz_rows")}
            log(f"{code}: {status}; tract changes {detail.get('changed_tract_rows')}; "
                f"oz changes {detail.get('changed_oz_rows')}")
        except Exception as exc:  # refusal or unexpected: never partial-write, keep going
            summary["refused"] += 1
            summary["counties"][code] = {"status": "refused", "error": f"{type(exc).__name__}: {exc}"}
            log(f"{code}: REFUSED: {exc}")
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--batch", required=True)
    parser.add_argument("--fips", nargs="*")
    parser.add_argument("--reclaim", action="store_true",
                        help="delete staged parquet payloads after hash-verified promotion")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    summary = promote_batch(ROOT, args.batch, args.fips, args.reclaim, args.dry_run)
    print(json.dumps({k: v for k, v in summary.items() if k != "counties"}, indent=2))
    return 1 if summary["refused"] else 0


if __name__ == "__main__":
    sys.exit(main())
