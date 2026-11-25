"""Generate docs/MISSING_COUNTIES.md — every county NOT in the dataset, with the reason.

Reads data/parcels/manifest.json (what we have) + src/county_methods.csv (probe method +
note per county). Reasons come from the crosswalk method, sharpened by the per-state lane
findings of the July 2026 rural-wave recons (recon/{ga,ky,ks,mo,ne}_rural_recon.md).
Re-run after any ingest wave:  python src/build_missing_report.py
"""
import csv
import datetime
import json
import os
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(ROOT, "docs", "MISSING_COUNTIES.md")

CATEGORY = {
    "web-app-only": "viewer-only",
    "none": "no public source",
    "suppressed": "owner-suppressed source",
    "arcgis": "mapped endpoint broken/rotted",
    "bulk-download": "bulk source unwired",
    "unprobed": "not yet probed",
    "excluded": "scope-excluded",
    "alias": "alias of ingested data",
}

# Reason sharpening from the 2026-07 recon waves — keyed by state, applies to the
# web-app-only tail unless a county-specific note says otherwise.
STATE_LANE = {
    "KY": "KY DOR holds a secured statewide PVA layer covering 105/120 counties — one "
          "data-sharing agreement (likely fee, KRS 133.047) unlocks most of this tail; "
          "BGADD/GRADD regional hosts were down mid-recon, retry (recon/ky_rural_recon.md)",
    "KS": "KS DASC/ORKA maintains a current statewide fabric (104/105 cos, parcels "
          "refreshed 2026-07) behind the KORA viewer — one records request unlocks the "
          "tail; Johnson (240k) = AIMS letter (recon/ks_rural_recon.md)",
    "GA": "qPublic/Schneider viewer tail — records-request tier or GA GIS Clearinghouse "
          "registration; June walls re-verified 2026-07-17 (recon/ga_rural_recon.md)",
    "MO": "vendor-token viewers (Integrity GIS/SAM, CamaVision, GIS Cloud); CAMA is still "
          "public record -> records tier; MSDIS statewide = dead end (recon/mo_rural_recon.md)",
    "CT": "NOT a real gap: 2022 planning-region FIPS aliases of the statewide layer "
          "already ingested under county FIPS — needs a region->county remap decision, "
          "not a new source",
    "PR": "scope-excluded by user decision 2026-07 (CRIM data exists; never re-propose)",
}


def main():
    manifest = json.load(open(os.path.join(ROOT, "data", "parcels", "manifest.json"),
                              encoding="utf-8"))
    rows = list(csv.DictReader(open(os.path.join(HERE, "county_methods.csv"),
                                    encoding="utf-8")))
    missing = sorted((r for r in rows if r["fips"] not in manifest),
                     key=lambda r: (r["state"], r["county"]))
    by_cat = defaultdict(int)
    by_state = defaultdict(list)
    for r in missing:
        by_cat[CATEGORY.get(r["method"], r["method"])] += 1
        by_state[r["state"]].append(r)

    with open(OUT, "w", encoding="utf-8") as f:
        w = f.write
        w("# Missing counties — what we don't have, and why\n\n")
        w(f"*Generated {datetime.date.today().isoformat()} from `data/parcels/manifest.json` "
          f"({len(manifest):,} counties ingested) + `src/county_methods.csv`. "
          f"Re-run `python src/build_missing_report.py` after any ingest wave.*\n\n")
        w(f"**{len(missing)} counties missing** "
          f"({len(missing) - len(by_state.get('PR', []))} excluding Puerto Rico).\n\n")
        w("| Reason | Counties |\n|---|---:|\n")
        for cat, n in sorted(by_cat.items(), key=lambda x: -x[1]):
            w(f"| {cat} | {n} |\n")
        w("\n**Reason legend.** *viewer-only*: parcels live in a hosted web viewer "
          "(qPublic/Beacon/Schneider/vendor portal) with no bulk/REST endpoint found as of "
          "the note date — the class that rots fastest (this week's waves flipped 150+ of "
          "these). *mapped endpoint broken/rotted*: we had a recipe; the service moved, "
          "renamed, or grew a token wall — needs re-recon, usually recoverable. "
          "*owner-suppressed source*: a public geometry layer exists but carries no owner "
          "— ingestable as a geometry-tier county (NJ/KY-class) whenever we choose. "
          "*no public source*: no digital endpoint found at all; records-request tier, "
          "and a few may have no digitized parcel fabric (frontier AK). "
          "*bulk source unwired / not yet probed*: known lead, no recipe written yet.\n\n")
        w("---\n\n")
        for st in sorted(by_state):
            rs = by_state[st]
            lane = STATE_LANE.get(st)
            w(f"## {st} — {len(rs)} missing\n\n")
            if lane:
                w(f"> **Lane:** {lane}\n\n")
            w("| FIPS | County | Reason | Note (probe history) |\n|---|---|---|---|\n")
            for r in rs:
                note = (r.get("note") or "").replace("|", "/").strip()
                if len(note) > 220:
                    note = note[:217] + "..."
                w(f"| {r['fips']} | {r['county']} | "
                  f"{CATEGORY.get(r['method'], r['method'])} | {note} |\n")
            w("\n")
    print(f"wrote {OUT}: {len(missing)} missing counties, "
          f"{len(by_state)} states/territories")


if __name__ == "__main__":
    main()
