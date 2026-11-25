"""County-scoped source identity and ownership-role preservation.

These fields describe source accounts, not independently verified land title.
Never collapse a special-interest account into its base account or pick an owner
solely because two accounts share a polygon.
"""
import pandas as pd


ROLE_COLUMNS = [
    "source_account_id", "source_parcel_id", "source_base_parcel_id",
    "source_taxlot_number", "source_special_interest_type",
    "source_special_interest_number", "source_owner_role",
    "source_owner_in_care_of", "source_owner_agent", "source_property_class",
    "source_rmv_property_class",
]
ROLE_FIPS = {"41003", "41007", "41059"}
# Public Tax_Lots schema, verified 2026-09-12. Require a map prefix, not Taxlot alone.
# Full-county staging 2026-09-13 also found 44 H-marked map prefixes,
# e.g. MAP=1S30H0000 + Taxlot=100 -> 1S30H000000100 (14-char base).
UMATILLA_KEY = (r"(?P<base>\d[NS]\d{2}H?\d{2}[A-Z0-9]{2}\d{5})"
                r"(?:(?P<kind>[A-Z])(?P<number>\d+))?")


def clean(series):
    return (series.astype("string").str.strip()
            .str.replace(r"\.0+$", "", regex=True)
            .mask(lambda s: s.str.lower().isin(["", "none", "null", "nan", "<na>"])))


def column(frame, name):
    return next((c for c in frame.columns if c.lower() == name.lower()), None)


def values(frame, *names):
    result = pd.Series(pd.NA, index=frame.index, dtype="string")
    for name in names:
        src = column(frame, name)
        if src:
            result = result.fillna(clean(frame[src]))
    return result


def mapping_entry(entry, frame):
    """Override stale manifest mappings without mutating the supplied manifest."""
    result = dict(entry)
    if entry["fips"] == "41059":
        owner = column(frame, "MAILING_NA")
        if not owner:
            raise ValueError("Umatilla source lacks MAILING_NA; refusing owner/agent fallback")
        result["owner_fields"] = owner
        result["value_field"] = "NONE"  # RMV_PROPER is a classification, not dollars.
    return result


def account_fields(frame, fips):
    result = pd.DataFrame({c: pd.Series(pd.NA, index=frame.index, dtype="string") for c in ROLE_COLUMNS})
    if fips not in ROLE_FIPS:
        return result
    if fips == "41059":
        if not any(column(frame, name) for name in ("map_tax", "TLID")):
            raise ValueError("Umatilla source lacks full map_tax/TLID; refusing bare Taxlot fallback")
        primary, alternate = values(frame, "map_tax"), values(frame, "TLID")
        mismatch = primary.notna() & alternate.notna() & primary.ne(alternate)
        if mismatch.any():
            raise ValueError("Umatilla map_tax/TLID disagree; source identity requires review")
        key = primary.fillna(alternate)
        if (key.notna() & ~key.str.fullmatch(UMATILLA_KEY, na=False)).any():
            raise ValueError("Umatilla map_tax/TLID contains an incomplete or invalid map/taxlot")
        kind = values(frame, "S_I_TYPE").str.upper()
        number = values(frame, "SI_NUMBER")
        suffix = key.str.extract("^" + UMATILLA_KEY + "$")
        # Parse AFTER the full 13/14-character base; map letters are not interests.
        suffix_kind, suffix_number = suffix["kind"], suffix["number"]
        if (kind.notna() & (suffix_kind.isna() | kind.ne(suffix_kind))).any():
            raise ValueError("Umatilla interest type disagrees with full source key")
        if (number.notna() & (suffix_number.isna() | number.ne(suffix_number))).any():
            raise ValueError("Umatilla interest number disagrees with full source key")
        result["source_parcel_id"] = key
        result["source_base_parcel_id"] = suffix["base"]
        result["source_special_interest_type"] = kind.fillna(suffix_kind)
        result["source_special_interest_number"] = number.fillna(suffix_number)
        result["source_account_id"] = values(frame, "account_id")
        result["source_taxlot_number"] = values(frame, "Taxlot")
        result["source_owner_in_care_of"] = values(frame, "IN_CARE_OF")
        result["source_owner_agent"] = values(frame, "AGENT")
        result["source_property_class"] = values(frame, "PROPERTY_C")
        result["source_rmv_property_class"] = values(frame, "RMV_PROPER")
        result["source_owner_role"] = "unknown"
        identified = key.notna() & result["source_account_id"].notna()
        result.loc[identified & result.source_special_interest_type.isna(), "source_owner_role"] = "base_account"
        result.loc[identified & result.source_special_interest_type.notna(), "source_owner_role"] = "special_interest_account"
    elif fips == "41003":
        result["source_account_id"] = values(frame, "Account_Num")
        result["source_parcel_id"] = values(frame, "MapTaxlot")
        result["source_owner_in_care_of"] = values(frame, "In_Care_Of")
        result["source_owner_role"] = "unknown"  # owner view lacks account-interest types
    else:
        result["source_account_id"] = values(frame, "ACCOUNT_ID")
        result["source_parcel_id"] = values(frame, "TAXLOTKEY", "MapTaxlot")
        result["source_owner_role"] = "unknown"  # machinery vs land needs assessor detail
    return result


def apply_account_fields(out, raw, fips, field_map):
    metadata = account_fields(raw, fips)
    for name in ROLE_COLUMNS:
        out[name] = metadata[name]
    if fips in ROLE_FIPS:
        field_map["source_account_roles"] = "county-scoped-v1"
    if fips == "41059":
        out["apn"] = metadata["source_parcel_id"]
        field_map["apn"] = "map_tax|TLID (validated full key; no bare Taxlot fallback)"
        field_map["assessed_value_blocked"] = "RMV_PROPER is Real Market Value Property Class"
    return out


def require_safe_overlay(fips):
    # Legacy captures could contain property codes and bare taxlot keys. Neither
    # capture nor replay is safe until separately validated/re-keyed value data exists.
    if fips == "41059":
        raise ValueError("Umatilla value overlay quarantined: audit dollar provenance and full account keys before enabling")
