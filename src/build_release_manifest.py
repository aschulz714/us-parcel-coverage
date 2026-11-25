#!/usr/bin/env python3
"""Build the canonical, fail-closed release manifest.

The gate uses Parquet footers rather than scanning row data. It reconciles raw
artifacts to source counts, verifies normalized artifact presence, checks source
rights metadata, records publication-policy application, and hashes release
control files plus locally present PMTiles.

Usage:
  python src/build_release_manifest.py
  python src/build_release_manifest.py --hash-artifacts
  python src/build_release_manifest.py --allow-blocked
"""
import argparse
import datetime
import glob
import hashlib
import json
import os
import subprocess
import shutil
import sys

import pyarrow.parquet as pq

from atomic_io import atomic_write_json
from publication_policy import DEFAULT_POLICY_PATH, load_policy, state_policy


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SOURCE_MANIFEST = os.path.join(ROOT, "data", "parcels", "manifest.json")
OUTPUT_DIR = os.path.join(ROOT, "output")
DEFAULT_OUTPUT = os.path.join(OUTPUT_DIR, "release_manifest.json")
REQUIRED_RIGHTS_FIELDS = (
    "license_url", "redistribution_allowed", "commercial_use_allowed",
    "privacy_classification",
)
PUBLISHABLE_PRIVACY_CLASSES = {"public", "public_with_suppression"}


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _relative(path, root):
    return os.path.relpath(path, root).replace("\\", "/")


def _resolve(root, value):
    if not value:
        return None
    return value if os.path.isabs(value) else os.path.join(root, value.replace("/", os.sep))


def _footer_rows(path):
    return int(pq.read_metadata(path).num_rows)


def _git_value(root, args):
    try:
        executable = shutil.which("git.exe") or shutil.which("git")
        if not executable or os.path.getsize(executable) == 0:
            return None
        result = subprocess.run(
            [executable] + args, cwd=root, capture_output=True, text=True, timeout=10)
        return result.stdout.strip() if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def build_release_manifest(root=ROOT, source_manifest_path=SOURCE_MANIFEST,
                           policy_path=DEFAULT_POLICY_PATH, output_dir=OUTPUT_DIR,
                           hash_artifacts=False):
    with open(source_manifest_path, encoding="utf-8") as handle:
        source_manifest = json.load(handle)
    policy = load_policy(policy_path)
    source_hash = sha256_file(source_manifest_path)
    policy_hash = sha256_file(policy_path)

    blockers = []
    warnings = []
    counties = {}
    totals = {
        "source_entries": len(source_manifest),
        "normalized_artifacts": 0,
        "normalized_rows": 0,
        "count_reconciled": 0,
        "count_unverified": 0,
        "rights_complete": 0,
        "rights_blocked": 0,
        "rights_unknown": 0,
    }

    for fips, entry in sorted(source_manifest.items()):
        raw_path = _resolve(root, entry.get("file"))
        norm_path = _resolve(root, entry.get("normalized_file"))
        expected = entry.get("expected_count")
        if expected is None:
            expected = entry.get("reported_count")
        raw_rows = _footer_rows(raw_path) if raw_path and os.path.exists(raw_path) else None
        norm_rows = _footer_rows(norm_path) if norm_path and os.path.exists(norm_path) else None
        issues = []

        if expected is None:
            issues.append("count_unverified")
            totals["count_unverified"] += 1
        elif raw_rows is None:
            issues.append("raw_artifact_missing")
        elif raw_rows != int(expected):
            issues.append("raw_count_mismatch")
        elif int(entry.get("skipped_count") or 0) != 0:
            issues.append("source_ids_skipped")
        else:
            totals["count_reconciled"] += 1

        if norm_rows is None:
            issues.append("normalized_artifact_missing")
        else:
            totals["normalized_artifacts"] += 1
            totals["normalized_rows"] += norm_rows
            recorded_norm = entry.get("normalized_row_count")
            if recorded_norm is not None and norm_rows != int(recorded_norm):
                issues.append("normalized_count_mismatch")

        rights_missing = [field for field in REQUIRED_RIGHTS_FIELDS
                          if entry.get(field) is None or entry.get(field) == ""]
        rights_issues = []
        if rights_missing:
            totals["rights_unknown"] += 1
            issues.append("source_rights_unknown")
        else:
            if entry.get("redistribution_allowed") is not True:
                rights_issues.append("redistribution_not_allowed_or_unverified")
            if entry.get("commercial_use_allowed") is not True:
                rights_issues.append("commercial_use_not_allowed_or_unverified")
            privacy_class = str(entry.get("privacy_classification") or "").strip().lower()
            if privacy_class not in PUBLISHABLE_PRIVACY_CLASSES:
                rights_issues.append("privacy_classification_not_publishable")
            if rights_issues:
                totals["rights_blocked"] += 1
                issues.extend(rights_issues)
            else:
                totals["rights_complete"] += 1

        county = {
            "state": entry.get("state"),
            "county": entry.get("county"),
            "source_url": entry.get("source_url"),
            "retrieved_at": entry.get("retrieved_at"),
            "expected_count": expected,
            "raw_rows": raw_rows,
            "normalized_rows": norm_rows,
            "count_status": entry.get("count_status") or
                            ("verified_by_release_scan" if expected is not None and
                             raw_rows == int(expected) else "legacy_unverified"),
            "skipped_count": int(entry.get("skipped_count") or 0),
            "rights_missing": rights_missing,
            "rights_status": ("unknown" if rights_missing else
                              "blocked" if rights_issues else "approved"),
            "issues": issues,
        }
        counties[fips] = county
        for issue in issues:
            blockers.append({"scope": fips, "issue": issue})

    states_path = os.path.join(output_dir, "states.json")
    states = {}
    if os.path.exists(states_path):
        with open(states_path, encoding="utf-8") as handle:
            states = json.load(handle)
    publication = {}
    for state_lower, state_entry in sorted(states.items()):
        state = state_lower.upper()
        rule = state_policy(policy, state)
        required_suppression = set(rule.get("exclude_fields") or [])
        recorded_suppression = set(state_entry.get("suppressed_fields") or [])
        missing_suppression = sorted(required_suppression - recorded_suppression)
        policy_version = state_entry.get("publication_policy_version")
        published_sensitive = sorted(required_suppression & set(state_entry.get("published_fields") or []))
        pmtiles_name = state_entry.get("pmtiles")
        pmtiles_path = os.path.join(output_dir, pmtiles_name) if pmtiles_name else None
        publication[state] = {
            "pmtiles": pmtiles_name,
            "required_suppressed_fields": sorted(required_suppression),
            "recorded_suppressed_fields": sorted(recorded_suppression),
            "missing_suppression": missing_suppression,
            "published_sensitive_fields": published_sensitive,
            "policy_version": policy_version,
            "policy_review_required": bool(rule.get("review_required")),
            "local_artifact_present": bool(pmtiles_path and os.path.exists(pmtiles_path)),
        }
        if pmtiles_name and required_suppression and policy_version != policy["schema_version"]:
            blockers.append({"scope": state, "issue": "publication_policy_not_applied",
                             "required_version": policy["schema_version"],
                             "artifact_version": policy_version})
        if pmtiles_name and missing_suppression:
            blockers.append({"scope": state, "issue": "publication_suppression_incomplete",
                             "fields": missing_suppression})
        if pmtiles_name and published_sensitive:
            blockers.append({"scope": state, "issue": "sensitive_fields_published",
                             "fields": published_sensitive})

    artifacts = {
        "source_manifest": {
            "path": _relative(source_manifest_path, root), "sha256": source_hash,
            "bytes": os.path.getsize(source_manifest_path),
        },
        "publication_policy": {
            "path": _relative(policy_path, root), "sha256": policy_hash,
            "bytes": os.path.getsize(policy_path),
        },
        "states": None,
        "pmtiles": [],
    }
    if os.path.exists(states_path):
        artifacts["states"] = {
            "path": _relative(states_path, root), "sha256": sha256_file(states_path),
            "bytes": os.path.getsize(states_path),
        }
    for path in sorted(glob.glob(os.path.join(output_dir, "*.pmtiles"))):
        artifacts["pmtiles"].append({
            "path": _relative(path, root),
            "bytes": os.path.getsize(path),
            "sha256": sha256_file(path) if hash_artifacts else None,
        })
    if artifacts["pmtiles"] and not hash_artifacts:
        blockers.append({"scope": "release", "issue": "pmtiles_hashes_omitted"})

    generated_at = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    gate_status = "passed" if not blockers else "blocked"
    return {
        "schema_version": 1,
        "release_id": f"{generated_at.replace(':', '').replace('-', '')}-{source_hash[:12]}",
        "generated_at": generated_at,
        "release_gate": {
            "status": gate_status,
            "blocker_count": len(blockers),
            "warning_count": len(warnings),
            "blockers": blockers,
            "warnings": warnings,
        },
        "code": {"commit": _git_value(root, ["rev-parse", "HEAD"])},
        "totals": totals,
        "source_governance": {
            "required_fields": list(REQUIRED_RIGHTS_FIELDS),
            "complete_counties": totals["rights_complete"],
            "unknown_counties": totals["rights_unknown"],
        },
        "publication": publication,
        "artifacts": artifacts,
        "counties": counties,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-manifest", default=SOURCE_MANIFEST)
    parser.add_argument("--policy", default=DEFAULT_POLICY_PATH)
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    parser.add_argument("--hash-artifacts", action="store_true")
    parser.add_argument("--allow-blocked", action="store_true",
                        help="write a blocked diagnostic manifest but exit zero")
    args = parser.parse_args()
    release = build_release_manifest(
        root=ROOT, source_manifest_path=args.source_manifest, policy_path=args.policy,
        output_dir=os.path.dirname(os.path.abspath(args.output)),
        hash_artifacts=args.hash_artifacts)
    atomic_write_json(args.output, release)
    gate = release["release_gate"]
    print(f"release gate: {gate['status'].upper()} | {gate['blocker_count']} blockers | "
          f"{release['totals']['normalized_rows']:,} normalized rows")
    print(f"wrote {_relative(args.output, ROOT)}")
    if gate["status"] != "passed" and not args.allow_blocked:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
