"""Fail-closed feature-count reconciliation and durable ingest audit records."""
import datetime
import os

from atomic_io import atomic_write_json


class ReconciliationError(RuntimeError):
    """A pull cannot be proven complete. ``audit`` is safe to persist."""
    def __init__(self, message, audit):
        super().__init__(message)
        self.audit = audit


def reconcile_feature_counts(fetched_count, reported_count=None, source_id_count=None,
                             skipped_ids=None, waiver_reason=None):
    """Return a count audit or fail when completeness cannot be proven.

    A waiver is accepted only when neither a count nor an object-ID universe is
    available. Count mismatches and skipped source IDs cannot be waived.
    """
    fetched = int(fetched_count)
    reported = int(reported_count) if reported_count is not None else None
    source_ids = int(source_id_count) if source_id_count is not None else None
    skipped = [str(value) for value in (skipped_ids or [])]
    expected = source_ids if source_ids is not None else reported
    audit = {
        "fetched_count": fetched,
        "reported_count": reported,
        "source_id_count": source_ids,
        "expected_count": expected,
        "count_delta": None if expected is None else fetched - expected,
        "skipped_count": len(skipped),
        "skipped_ids": skipped,
        "count_status": "failed",
    }
    if skipped:
        raise ReconciliationError(
            f"incomplete pull: {len(skipped)} source IDs were skipped", audit)
    expected_values = {"reported_count": reported, "source_id_count": source_ids}
    mismatches = {name: value for name, value in expected_values.items()
                  if value is not None and value != fetched}
    if mismatches:
        detail = ", ".join(f"{name}={value}" for name, value in mismatches.items())
        raise ReconciliationError(
            f"count reconciliation failed: fetched_count={fetched}, {detail}", audit)
    if expected is None:
        if not waiver_reason or not waiver_reason.strip():
            raise ReconciliationError(
                "source count is unavailable; provide a reviewed count-waiver reason to proceed",
                audit)
        audit["count_status"] = "unverified_waiver"
        audit["count_waiver_reason"] = waiver_reason.strip()
        return audit
    audit["count_status"] = "verified"
    return audit


def write_ingest_audit(root, raw_dir, fips, audit):
    """Persist a success or failure audit outside the replaceable raw path."""
    stamped = dict(audit)
    stamped.setdefault("recorded_at", datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"))
    audit_dir = os.path.join(os.path.dirname(raw_dir), "ingest_audit")
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    path = os.path.join(audit_dir, f"{fips}_{stamp}_{os.getpid()}.json")
    atomic_write_json(path, stamped)
    return os.path.relpath(path, root).replace("\\", "/")
