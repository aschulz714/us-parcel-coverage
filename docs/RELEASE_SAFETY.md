# Release safety gate

The P0 stabilization controls make publication fail closed. They do not upload,
delete, or mutate remote artifacts.

## What is enforced

- `src/ingest.py` promotes a raw county only when fetched rows exactly reconcile
  to both the source count and, when used, the object-ID universe.
- Skipped object IDs and every reconciliation outcome are written to
  `data/parcels/ingest_audit/` and referenced by successful manifest entries.
- A count-less source requires `--count-waiver-reason`; mismatched counts and
  skipped IDs cannot be waived. Waived pulls remain visible as unverified.
- Normalized data, value overlays, column enrichments, and `states.json` use
  same-volume temporary files followed by atomic replacement.
- A registered value overlay that cannot be read or applied fails normalization.
- `config/publication_policy.json` controls fields permitted in PMTiles. New
  Jersey currently suppresses `owner`, `owner_norm`, `owner_family`, and
  `situs_address` pending privacy review and a protected-address workflow.
- `src/build_release_manifest.py` reconciles artifacts, checks source-rights
  metadata and permissions, checks publication-policy versions and suppression
  records, records code/data hashes, and exits nonzero while blockers remain.

## Required source-rights metadata

Every county source must eventually record these manifest fields:

- `license_url`
- `redistribution_allowed`
- `commercial_use_allowed`
- `privacy_classification`

Unknown rights block publication; they do not prevent internal diagnostic use.
Explicitly disallowed or unverified redistribution/commercial-use values also
block publication. Privacy classification must be `public` or
`public_with_suppression`, and locally staged PMTiles must be hashed.

## Release sequence

```powershell
python qa/test_release_safety.py
python qa/test_pipeline_synthetic.py
python qa/validate.py
python src/build_tiles.py --state NJ
python src/build_release_manifest.py --hash-artifacts
```

Run the manifest builder with `--allow-blocked` only to generate a diagnostic
report. A blocked report is not publication approval.
