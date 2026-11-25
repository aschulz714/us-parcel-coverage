"""Load and enforce field-level publication rules for parcel artifacts."""
import json
import os


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_POLICY_PATH = os.path.join(ROOT, "config", "publication_policy.json")


def load_policy(path=DEFAULT_POLICY_PATH):
    if not os.path.exists(path):
        raise RuntimeError(f"publication policy missing: {path}")
    with open(path, encoding="utf-8") as handle:
        policy = json.load(handle)
    if policy.get("schema_version") != 1:
        raise RuntimeError("unsupported publication policy schema_version")
    if not isinstance(policy.get("default"), dict) or not isinstance(policy.get("states"), dict):
        raise RuntimeError("publication policy requires default and states objects")
    return policy


def state_policy(policy, state):
    """Return the merged default + state rule for a two-letter state code."""
    rule = dict(policy.get("default") or {})
    rule.update((policy.get("states") or {}).get(state.upper(), {}))
    rule["state"] = state.upper()
    return rule


def filter_fields(policy, state, desired_fields, available_fields):
    """Return (published, suppressed, rule), preserving desired field order."""
    rule = state_policy(policy, state)
    excluded = set(rule.get("exclude_fields") or [])
    available = set(available_fields)
    published = [field for field in desired_fields if field in available and field not in excluded]
    suppressed = [field for field in desired_fields if field in available and field in excluded]
    return published, suppressed, rule

