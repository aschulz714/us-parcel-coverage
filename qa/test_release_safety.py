#!/usr/bin/env python3
"""Fast regression tests for the P0 release-safety controls."""
import json
import os
import shutil
import sys
import tempfile
import unittest

import pandas as pd


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from atomic_io import atomic_replace_writer, atomic_write_json
from build_release_manifest import build_release_manifest
from ingest_integrity import ReconciliationError, reconcile_feature_counts
from publication_policy import filter_fields, load_policy


class ReleaseSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.mkdtemp(prefix="npc_release_safety_")

    def tearDown(self):
        shutil.rmtree(self.temp, ignore_errors=True)

    def test_new_jersey_sensitive_tile_fields_are_suppressed(self):
        policy = load_policy(os.path.join(ROOT, "config", "publication_policy.json"))
        desired = ["apn", "owner", "owner_norm", "owner_family", "situs_address", "acreage"]
        published, suppressed, _ = filter_fields(policy, "NJ", desired, desired)
        self.assertEqual(published, ["apn", "acreage"])
        self.assertEqual(
            suppressed, ["owner", "owner_norm", "owner_family", "situs_address"])
        published_wa, suppressed_wa, _ = filter_fields(policy, "WA", desired, desired)
        self.assertEqual(published_wa, desired)
        self.assertEqual(suppressed_wa, [])

    def test_count_reconciliation_is_fail_closed(self):
        audit = reconcile_feature_counts(100, reported_count=100)
        self.assertEqual(audit["count_status"], "verified")
        with self.assertRaises(ReconciliationError):
            reconcile_feature_counts(99, reported_count=100)
        with self.assertRaises(ReconciliationError):
            reconcile_feature_counts(99, source_id_count=100, skipped_ids=["42"])
        with self.assertRaises(ReconciliationError):
            reconcile_feature_counts(100)
        waived = reconcile_feature_counts(100, waiver_reason="Source exposes no count endpoint")
        self.assertEqual(waived["count_status"], "unverified_waiver")

    def test_atomic_writer_preserves_last_good_file_on_failure(self):
        target = os.path.join(self.temp, "artifact.txt")
        with open(target, "w", encoding="utf-8") as handle:
            handle.write("last-good")

        def broken_writer(temp_path):
            with open(temp_path, "w", encoding="utf-8") as handle:
                handle.write("partial")
            raise RuntimeError("simulated disk/write failure")

        with self.assertRaises(RuntimeError):
            atomic_replace_writer(target, broken_writer)
        with open(target, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "last-good")

    def test_release_gate_checks_counts_rights_and_publication_policy(self):
        raw_dir = os.path.join(self.temp, "data", "parcels", "raw")
        norm_dir = os.path.join(self.temp, "data", "parcels", "normalized")
        output_dir = os.path.join(self.temp, "output")
        config_dir = os.path.join(self.temp, "config")
        for directory in (raw_dir, norm_dir, output_dir, config_dir):
            os.makedirs(directory, exist_ok=True)
        raw = os.path.join(raw_dir, "34001_test.parquet")
        norm = os.path.join(norm_dir, "34001_test.parquet")
        pd.DataFrame({"apn": ["1", "2"]}).to_parquet(raw, index=False)
        pd.DataFrame({"apn": ["1", "2"]}).to_parquet(norm, index=False)
        source_manifest = os.path.join(self.temp, "data", "parcels", "manifest.json")
        atomic_write_json(source_manifest, {
            "34001": {
                "state": "NJ", "county": "Test", "source_url": "synthetic://source",
                "retrieved_at": "2026-07-21T00:00:00Z",
                "file": "data/parcels/raw/34001_test.parquet",
                "normalized_file": "data/parcels/normalized/34001_test.parquet",
                "reported_count": 2, "expected_count": 2, "fetched_count": 2,
                "skipped_count": 0, "count_status": "verified",
                "license_url": "synthetic://license", "redistribution_allowed": True,
                "commercial_use_allowed": True, "privacy_classification": "public",
            }
        })
        policy_path = os.path.join(config_dir, "publication_policy.json")
        with open(os.path.join(ROOT, "config", "publication_policy.json"), encoding="utf-8") as handle:
            atomic_write_json(policy_path, json.load(handle))
        states_path = os.path.join(output_dir, "states.json")
        atomic_write_json(states_path, {"nj": {
            "pmtiles": "parcels_nj.pmtiles",
            "publication_policy_version": 1,
            "published_fields": ["apn"],
            "suppressed_fields": ["owner", "owner_norm", "owner_family", "situs_address"],
        }})
        release = build_release_manifest(
            root=self.temp, source_manifest_path=source_manifest,
            policy_path=policy_path, output_dir=output_dir)
        self.assertEqual(release["release_gate"]["status"], "passed")
        with open(states_path, encoding="utf-8") as handle:
            states = json.load(handle)
        states["nj"]["publication_policy_version"] = 0
        atomic_write_json(states_path, states)
        blocked = build_release_manifest(
            root=self.temp, source_manifest_path=source_manifest,
            policy_path=policy_path, output_dir=output_dir)
        issues = [item["issue"] for item in blocked["release_gate"]["blockers"]]
        self.assertIn("publication_policy_not_applied", issues)

        states["nj"]["publication_policy_version"] = 1
        states["nj"]["suppressed_fields"] = ["owner"]
        atomic_write_json(states_path, states)
        blocked = build_release_manifest(
            root=self.temp, source_manifest_path=source_manifest,
            policy_path=policy_path, output_dir=output_dir)
        issues = [item["issue"] for item in blocked["release_gate"]["blockers"]]
        self.assertIn("publication_suppression_incomplete", issues)

        with open(source_manifest, encoding="utf-8") as handle:
            sources = json.load(handle)
        sources["34001"]["redistribution_allowed"] = False
        atomic_write_json(source_manifest, sources)
        states["nj"]["suppressed_fields"] = [
            "owner", "owner_norm", "owner_family", "situs_address"]
        atomic_write_json(states_path, states)
        blocked = build_release_manifest(
            root=self.temp, source_manifest_path=source_manifest,
            policy_path=policy_path, output_dir=output_dir)
        issues = [item["issue"] for item in blocked["release_gate"]["blockers"]]
        self.assertIn("redistribution_not_allowed_or_unverified", issues)


if __name__ == "__main__":
    unittest.main(verbosity=2)
