"""Build the master coverage catalog (Markdown + interactive HTML) from the manifest.

The manifest (data/parcels/manifest.json) is the single source of truth: one entry per
ingested county carrying its source endpoint, field mapping, parcel count, vintage, and
per-attribute coverage. This rolls that up into two browsable artifacts that never drift
apart (same data, one generator) and never go stale (re-run after any ingest):

Outputs:
  docs/COVERAGE_CATALOG.md       — national summary + per-state county tables (browse/share on GitHub)
  output/coverage_catalog.html   — self-contained sortable/filterable dashboard (no dependencies)

Honesty (built into the output):
  - Geometry coverage is NOT tracked in the manifest. Last full measurement was ~99.6%
    (docs/COVERAGE_ASSESSMENT_2026-06-21.md); we state that rather than invent a column.
  - Land-use is tracked on only ~half the counties. Untracked counties show "-" in the
    per-county tables and count as 0 in the parcel-weighted rollups (a documented FLOOR,
    not a per-county claim of 0%).
  - Owner mailing-STATE is effectively unbuilt (a handful of counties) and is reported as
    such, not mixed into the headline coverage.

Usage:
  python src/build_coverage_catalog.py
"""
import argparse, csv, glob, json, os, sys
from collections import Counter, defaultdict

HERE = os.path.dirname(__file__)
ROOT = os.path.dirname(HERE)
MANIFEST = os.path.join(ROOT, "data", "parcels", "manifest.json")
COUNTIES = os.path.join(ROOT, "data", "us_counties.geojson")
CROSSWALK = os.path.join(HERE, "county_methods.csv")
UNIVERSE_EST = os.path.join(ROOT, "data", "national_universe.json")
DOCS = os.path.join(ROOT, "docs")
OUT = os.path.join(ROOT, "output")

# Last full geometry measurement (not tracked per-county in the manifest); stated, not guessed.
GEOMETRY_NOTE = "Geometry coverage is not tracked per-county in the manifest; last full measurement was ~99.6% (2026-06-21 assessment)."

STATE_NAMES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois",
    "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana",
    "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon",
    "PA": "Pennsylvania", "PR": "Puerto Rico", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia",
    "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
}

# Coverage dimensions read from manifest "populated" (assessed_value -> "value" for brevity).
# land_use is handled separately (top-level "landuse_coverage", tracked on a subset of counties).
POP_DIMS = [("apn", "apn"), ("owner", "owner"), ("assessed_value", "value"),
            ("situs_address", "situs"), ("mailing_address", "mail")]

# WHY a county isn't ingested yet -- read from the crosswalk "method" (the RECORDED source
# classification), never inferred. Each method maps to a human label + a bucket for
# grouping/filtering. "backlog" = a source is already mapped, so it's ingestable (the actionable
# gap); the others are genuine blockers (or not-yet-investigated). The crosswalk "note" carries
# the county-specific detail behind each.
MISSING_REASON = {
    "arcgis":        ("Source mapped — not yet ingested", "backlog"),
    "arcgis-join":   ("Source mapped (table join) — not yet ingested", "backlog"),
    "bulk-download": ("Bulk source identified — not yet ingested", "backlog"),
    "web-app-only":  ("Viewer-only source — no machine-queryable endpoint", "viewer"),
    "suppressed":    ("Owner names suppressed/redacted at source", "suppressed"),
    "none":          ("No public parcel source exists", "nosource"),
    "unprobed":      ("Source not yet probed", "unprobed"),
}
DEFAULT_REASON = ("Reason not recorded in crosswalk", "other")
GROUP_LABEL = {
    "backlog": "Source mapped (backlog)",
    "viewer": "Viewer-only, no endpoint",
    "suppressed": "Owner suppressed at source",
    "nosource": "No public source exists",
    "unprobed": "Not yet probed",
    "other": "Reason not recorded",
}
GROUP_ORDER = ["backlog", "viewer", "suppressed", "nosource", "unprobed", "other"]


# Jurisdictions deliberately OUT OF PROJECT SCOPE (owner decision, 2026-07-02: PR excluded --
# not a target market; its only source was a stale 2018 CRIM snapshot). Excluded from the
# county universe so denominators/missing lists reflect the actual project scope.
SCOPE_EXCLUDE = {"PR"}


def load_county_universe():
    """The full set of US counties per state, as {abbr: {fips: name}} plus a national count.

    This is both the denominator for completeness (e.g. NJ '5 / 21') AND the source of the
    per-state "missing" lists (universe minus what the manifest holds). Mirrors build_map_data.py:
    fips2 -> state from the crosswalk, over the national county geometry. Degrades gracefully to
    ({}, 0) if either reference file is absent (missing lists then come out empty)."""
    if not (os.path.exists(COUNTIES) and os.path.exists(CROSSWALK)):
        return {}, 0
    with open(CROSSWALK, encoding="utf-8") as f:
        st_by_fips2 = {r["fips"][:2]: r["state"].upper()
                       for r in csv.DictReader(f) if r.get("fips")}
    with open(COUNTIES, encoding="utf-8") as f:
        cj = json.load(f)
    universe = defaultdict(dict)
    national = 0
    for feat in cj["features"]:
        p = feat["properties"]
        st = st_by_fips2.get(p["STATE"])
        if st in SCOPE_EXCLUDE:
            continue
        if st:
            universe[st][p["STATE"] + p["COUNTY"]] = p["NAME"]
            national += 1
    return dict(universe), national


def _disk_rows(e):
    """Parcel count from the normalized parquet footer — DISK TRUTH. The manifest's
    feature_count keeps source-count semantics for refresh staleness, but 13 intentionally
    deduped counties (Cullman-class) make it overstate the dataset by ~63k; the catalog
    must report what is actually on disk."""
    nf = e.get("normalized_file")
    if not nf:
        return None
    try:
        import pyarrow.parquet as pq
        return pq.read_metadata(os.path.join(ROOT, nf)).num_rows
    except Exception:
        return None


def county_record(fips, e):
    """Flatten one manifest entry into the row the catalog renders (coverage as 0-1 or None)."""
    pop = e.get("populated") or {}
    src = e.get("source_url") or ""
    host = src.split("/")[2] if "://" in src else ""
    disk = _disk_rows(e)
    rec = {
        "fips": fips,
        "county": e.get("county") or "?",
        "state": (e.get("state") or "?").upper(),
        "parcels": disk if disk is not None else (e.get("feature_count") or 0),
        "method": e.get("method") or "",
        "host": host,
        "url": src,
        "vintage": e.get("source_vintage") or e.get("source_last_edit") or "",
        "pulled": (e.get("retrieved_at") or "")[:10],
        "note": e.get("note") or "",
        "lu": e.get("landuse_coverage"),          # None when untracked -> rendered as "-"
        "mail_state": pop.get("mail_state"),       # tracked on a handful of counties only
    }
    for src_key, out_key in POP_DIMS:
        rec[out_key] = pop.get(src_key)
    return rec


def wavg(rows, key):
    """Parcel-weighted coverage. Numerator counts only counties where the attribute is
    measured; denominator is ALL parcels in the group, so untracked counties pull the
    figure down to an honest floor rather than being silently excluded."""
    den = sum(r["parcels"] for r in rows)
    if not den:
        return None
    num = sum(r["parcels"] * r[key] for r in rows if r.get(key) is not None)
    return num / den


def rollup(rows, abbr=None, totals=None):
    """State- or nation-level aggregate over a list of county records."""
    lu = wavg(rows, "lu")
    agg = {
        "parcels": sum(r["parcels"] for r in rows),
        "counties_in": len(rows),
        "lu_tracked": sum(1 for r in rows if r["lu"] is not None),
        "ms_tracked": sum(1 for r in rows if r["mail_state"] is not None),
        "pull_max": max((r["pulled"] for r in rows if r["pulled"]), default=""),
        # Land use rolls up as a parcel-weighted floor over ALL parcels. Show "-" when that floor
        # rounds to 0 (no positive coverage established): a state measured only on a few empty
        # counties -- or none at all -- must not read as a confirmed 0%. Per-county cells keep
        # their measured 0 (we did check that county; it is genuinely empty).
        "lu": lu if (lu is not None and round(lu * 100) > 0) else None,
    }
    for _, k in POP_DIMS:
        agg[k] = wavg(rows, k)
    if abbr is not None:
        agg["abbr"] = abbr
        agg["name"] = STATE_NAMES.get(abbr, abbr)
        agg["counties_total"] = (totals or {}).get(abbr, 0)
    return agg


def build_model():
    with open(MANIFEST, encoding="utf-8") as f:
        manifest = json.load(f)
    universe, national_total = load_county_universe()
    totals = {abbr: len(fmap) for abbr, fmap in universe.items()}
    present_fips = set(manifest)
    with open(CROSSWALK, encoding="utf-8") as f:
        xwalk = {r["fips"]: r for r in csv.DictReader(f) if r.get("fips")}

    counties = sorted((county_record(fips, e) for fips, e in manifest.items()),
                      key=lambda r: (r["state"], -r["parcels"]))
    for r in counties:
        r["status"] = "in"
    by_state = defaultdict(list)
    for r in counties:
        by_state[r["state"]].append(r)

    # missing = the county universe minus the manifest. The "why" is read from the crosswalk
    # "method" (a recorded source classification, not inferred); the crosswalk note adds the
    # county-specific detail. Computed for EVERY state so the totally-absent ones (CA/HI/ME/PR)
    # still surface their full gap. Sorted by reason bucket, then name.
    def miss_entry(fips, name):
        x = xwalk.get(fips, {})
        label, group = MISSING_REASON.get((x.get("method") or "").strip(), DEFAULT_REASON)
        return {"fips": fips, "county": name, "reason": label, "group": group,
                "note": (x.get("note") or "").strip()}
    missing_by_state = {
        abbr: sorted((miss_entry(f, nm) for f, nm in fmap.items() if f not in present_fips),
                     key=lambda m: (GROUP_ORDER.index(m["group"]), m["county"]))
        for abbr, fmap in universe.items()
    }

    states = []
    for abbr in set(by_state) | set(universe):
        s = rollup(by_state.get(abbr, []), abbr, totals)
        miss = missing_by_state.get(abbr, [])
        s["missing_n"] = len(miss)
        s["backlog_n"] = sum(1 for m in miss if m["group"] == "backlog")
        states.append(s)
    states.sort(key=lambda s: (-s["parcels"], s["name"]))

    all_missing = [m for ms in missing_by_state.values() for m in ms]
    groups = Counter(m["group"] for m in all_missing)
    national = rollup(counties)
    national["counties_total"] = national_total
    national["jurisdictions"] = len(by_state)
    national["missing_n"] = len(all_missing)
    national["backlog_n"] = groups.get("backlog", 0)
    national["missing_breakdown"] = [[GROUP_LABEL[g], groups[g]] for g in GROUP_ORDER if groups.get(g)]
    national["as_of"] = national["pull_max"]
    # Headline OUTCOME metric: share of the ESTIMATED US parcel universe (docs/COVERAGE_TARGET.md).
    # County counts measure sources ingested; this measures how much of the country's parcels we
    # hold. The denominator is an estimate (data/national_universe.json) so it renders as "est."
    try:
        with open(UNIVERSE_EST, encoding="utf-8") as f:
            uni = json.load(f)
        national["universe_est"] = uni["estimated_us_parcels"]
        national["universe_pct"] = national["parcels"] / uni["estimated_us_parcels"]
    except (OSError, KeyError, ValueError, ZeroDivisionError):
        national["universe_est"] = national["universe_pct"] = None
    return {"national": national, "states": states, "counties": counties,
            "missing_by_state": missing_by_state}


# ---------- formatting helpers ----------

def ipct(x):
    """Integer percent for compact table cells; '-' when the attribute is not tracked."""
    return "-" if x is None else f"{round(x * 100)}"


def fpct(x):
    return "n/a" if x is None else f"{x * 100:.1f}%"


def commas(n):
    return f"{n:,}"


def cofrac(agg):
    t = agg.get("counties_total") or 0
    return f"{agg['counties_in']} / {t}" if t else f"{agg['counties_in']}"


# ---------- Markdown ----------

def write_markdown(model):
    nat = model["national"]
    L = []
    L.append("# National Parcel Coverage — Master Catalog\n")
    L.append(f"*Generated from `data/parcels/manifest.json` (the single source of truth). "
             f"Data current through **{nat['as_of']}**. Re-run `python src/build_coverage_catalog.py` to refresh.*\n")

    L.append("## National summary\n")
    n_dc = sum(1 for s in model["states"] if s["abbr"] == "DC")
    juris = f"{nat['jurisdictions']} ({nat['jurisdictions'] - n_dc} states + DC)" if n_dc else f"{nat['jurisdictions']} states"
    L.append(f"- **Parcels:** {commas(nat['parcels'])}")
    if nat.get("universe_pct"):
        L.append(f"- **Est. share of US parcel universe:** ~{round(100 * nat['universe_pct'])}% "
                 f"(denominator ~{commas(nat['universe_est'])} is an estimate — "
                 f"see `data/national_universe.json` and `docs/COVERAGE_TARGET.md`)")
    L.append(f"- **Jurisdictions:** {juris}")
    cofrac_pct = f" (~{round(100 * nat['counties_in'] / nat['counties_total'])}%)" if nat.get("counties_total") else ""
    L.append(f"- **Counties:** {cofrac(nat)}{cofrac_pct} — **{commas(nat['missing_n'])} not yet ingested**")
    if nat.get("missing_breakdown"):
        L.append("- **Why missing:** " + " · ".join(
            f"{cnt:,} {lbl.lower()}" for lbl, cnt in nat["missing_breakdown"]))
    L.append(f"- **Parcel-weighted coverage:** APN {fpct(nat['apn'])} · Owner {fpct(nat['owner'])} "
             f"· Value {fpct(nat['value'])} · Situs {fpct(nat['situs'])} "
             f"· Mailing {fpct(nat['mail'])} · Land use {fpct(nat['lu'])}\n")

    L.append("> **What's tracked, and what isn't.** APN, owner, assessed value, and situs/mailing "
             "address come straight from each county's measured coverage. "
             f"{GEOMETRY_NOTE} **Land use** is tracked on **{nat['lu_tracked']} of {nat['counties_in']}** "
             "ingested counties — untracked counties show `-` below and count as 0 in the weighted "
             "rollups (a floor, not a confirmed 0%). **Owner mailing-state** is effectively unbuilt "
             f"({nat['ms_tracked']} counties) and is excluded from the headline figures.\n")

    L.append("> **Value counts a *non-null* `assessed_value`, which includes parcels stamped `$0`.** "
             "*Usable* (positive) value coverage is lower — a direct ground-truth scan of the "
             "normalized parquets on 2026-07-03 measured **~80% of parcels carrying a value > 0** "
             "(vs the non-null figure above). The largest remaining gaps are **Iowa** (26% positive), "
             "unrecovered county blocks in **TX/OH/IL/MO/IN**, and the partial **CA** ingest. Use the "
             "positive-value figure when reasoning about how much of the dataset can actually answer "
             "a value question.\n")

    L.append("## States\n")
    L.append("*Coverage columns are % of parcels with that attribute populated; `-` = not tracked. "
             "**Backlog** = missing counties whose source is already mapped (ingestable). "
             "Click a state to jump to its counties.*\n")
    L.append("| State | Counties | Missing | Backlog | Parcels | APN | Owner | Value | Land use | Latest pull |")
    L.append("|-------|---------:|--------:|--------:|--------:|----:|------:|------:|---------:|:-----------:|")
    for s in model["states"]:
        anchor = s["name"].lower().replace(" ", "-")
        L.append(f"| [{s['name']}](#{anchor}) | {cofrac(s)} | {commas(s['missing_n'])} | {commas(s['backlog_n'])} | {commas(s['parcels'])} | "
                 f"{ipct(s['apn'])} | {ipct(s['owner'])} | {ipct(s['value'])} | "
                 f"{ipct(s['lu'])} | {s['pull_max'] or '-'} |")
    L.append("")

    missing_by_state = model.get("missing_by_state", {})
    by_state = defaultdict(list)
    for r in model["counties"]:
        by_state[r["state"]].append(r)
    for s in model["states"]:
        rows = by_state.get(s["abbr"], [])
        miss = missing_by_state.get(s["abbr"], [])
        L.append("\n---\n")
        L.append(f"### {s['name']}\n")
        L.append(f"*{commas(s['parcels'])} parcels · {cofrac(s)} counties ingested · "
                 f"{commas(s['missing_n'])} missing · land-use tracked on {s['lu_tracked']} counties*\n")
        if rows:
            L.append("| County | Parcels | Source | APN | Own | Val | LU | Pulled |")
            L.append("|--------|--------:|--------|----:|----:|----:|---:|:------:|")
            for r in rows:
                source = r["host"] or r["method"] or "-"
                L.append(f"| {r['county']} | {commas(r['parcels'])} | {source} | "
                         f"{ipct(r['apn'])} | {ipct(r['owner'])} | {ipct(r['value'])} | "
                         f"{ipct(r['lu'])} | {r['pulled']} |")
            L.append("")
        if miss:
            L.append(f"**Missing ({len(miss)})** — grouped by reason:\n")
            grouped = defaultdict(list)
            for m in miss:
                grouped[m["group"]].append(m["county"])
            for g in GROUP_ORDER:
                if grouped.get(g):
                    L.append(f"- *{GROUP_LABEL[g]} ({len(grouped[g])}):* " + ", ".join(grouped[g]))
            L.append("")
        elif rows:
            L.append("*All counties ingested.*\n")
    L.append("")

    os.makedirs(DOCS, exist_ok=True)
    path = os.path.join(DOCS, "COVERAGE_CATALOG.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print(f"  markdown -> {os.path.relpath(path, ROOT)} ({os.path.getsize(path) / 1024:,.0f} KB)")


# ---------- HTML ----------

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>National Parcel Coverage - Master Catalog</title>
<style>
  :root { --good:#1a7f37; --ok:#9a6700; --low:#cf222e; --na:#8c959f; --bg:#f6f8fa; --line:#d0d7de; }
  * { box-sizing: border-box; }
  body { margin:0; font:14px/1.45 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif; color:#1f2328; }
  header { padding:20px 24px; border-bottom:1px solid var(--line); background:#fff; }
  h1 { margin:0 0 4px; font-size:20px; }
  .sub { color:#57606a; font-size:13px; }
  .cards { display:flex; flex-wrap:wrap; gap:12px; padding:16px 24px; background:var(--bg); border-bottom:1px solid var(--line); }
  .card { background:#fff; border:1px solid var(--line); border-radius:8px; padding:10px 14px; min-width:120px; }
  .card .v { font-size:20px; font-weight:600; }
  .card .l { font-size:12px; color:#57606a; }
  .note { padding:12px 24px; background:#fff8c5; border-bottom:1px solid var(--line); font-size:12.5px; color:#4d3800; }
  .wrap { padding:16px 24px; }
  h2 { font-size:15px; margin:18px 0 8px; }
  .controls { display:flex; flex-wrap:wrap; gap:10px; align-items:center; margin:8px 0 12px; }
  input,select { font:13px inherit; padding:6px 8px; border:1px solid var(--line); border-radius:6px; }
  input[type=search] { min-width:240px; }
  .count { color:#57606a; font-size:12.5px; }
  a.reset { font-size:12.5px; cursor:pointer; color:#0969da; }
  table { border-collapse:collapse; width:100%; font-size:13px; }
  th,td { padding:5px 8px; border-bottom:1px solid #eaeef2; text-align:left; white-space:nowrap; }
  th { position:sticky; top:0; background:#fff; cursor:pointer; user-select:none; border-bottom:2px solid var(--line); }
  th.num,td.num { text-align:right; font-variant-numeric:tabular-nums; }
  th[aria-sort=ascending]::after { content:" \2191"; } th[aria-sort=descending]::after { content:" \2193"; }
  tbody tr:hover { background:#f6f8fa; }
  tr.state-row { cursor:pointer; }
  .pill { display:inline-block; min-width:30px; text-align:center; padding:1px 6px; border-radius:10px; font-variant-numeric:tabular-nums; }
  .good { background:#dafbe1; color:var(--good); } .ok { background:#fff1c2; color:var(--ok); }
  .low { background:#ffebe9; color:var(--low); } .na { color:var(--na); }
  .host { color:#57606a; } a.host { color:#0969da; text-decoration:none; } a.host:hover { text-decoration:underline; }
  .scroll { max-height:70vh; overflow:auto; border:1px solid var(--line); border-radius:8px; }
  .muted { color:#8c959f; }
</style>
</head>
<body>
<header>
  <h1>National Parcel Coverage &mdash; Master Catalog</h1>
  <div class="sub" id="sub"></div>
</header>
<div class="cards" id="cards"></div>
<div class="note" id="note"></div>
<div class="wrap">
  <h2>States</h2>
  <div class="scroll"><table id="states"><thead></thead><tbody></tbody></table></div>

  <h2>Counties</h2>
  <div class="controls">
    <input type="search" id="q" placeholder="Search county, state, or source...">
    <select id="stsel"></select>
    <select id="stat">
      <option value="">All counties</option>
      <option value="in">Ingested only</option>
      <optgroup label="Missing">
        <option value="missing">Missing &mdash; all</option>
        <option value="missing:backlog">Missing &mdash; source mapped (backlog)</option>
        <option value="missing:viewer">Missing &mdash; viewer-only</option>
        <option value="missing:suppressed">Missing &mdash; owner suppressed</option>
        <option value="missing:nosource">Missing &mdash; no source</option>
        <option value="missing:unprobed">Missing &mdash; not probed</option>
      </optgroup>
    </select>
    <span class="count" id="ccount"></span>
    <a class="reset" id="reset">reset</a>
  </div>
  <div class="scroll"><table id="counties"><thead></thead><tbody></tbody></table></div>
</div>
<script>
const M = __DATA__;

const pctCell = v => {
  if (v === null || v === undefined) return '<span class="pill na">-</span>';
  const p = Math.round(v * 100);
  const c = p >= 90 ? 'good' : p >= 60 ? 'ok' : 'low';
  return `<span class="pill ${c}">${p}</span>`;
};
const num = n => (n ?? 0).toLocaleString();
const cofrac = a => a.counties_total ? `${a.counties_in} / ${a.counties_total}` : `${a.counties_in}`;
const esc = s => (s || '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

// header card summary
document.getElementById('sub').innerHTML =
  `Generated from <code>data/parcels/manifest.json</code> &middot; data current through <b>${M.national.as_of}</b>`;
const n = M.national;
const cardDefs = [
  ['Parcels', num(n.parcels)],
  ['Est. US universe', n.universe_pct ? '~' + Math.round(n.universe_pct*100) + '%' : '-'],
  ['Jurisdictions', n.jurisdictions],
  ['Counties', cofrac(n)], ['Counties missing', num(n.missing_n)],
  ['APN', (n.apn*100).toFixed(1)+'%'], ['Owner', (n.owner*100).toFixed(1)+'%'],
  ['Value', (n.value*100).toFixed(1)+'%'], ['Land use', (n.lu*100).toFixed(1)+'%'],
];
document.getElementById('cards').innerHTML = cardDefs.map(([l,v]) =>
  `<div class="card"><div class="v">${v}</div><div class="l">${l}</div></div>`).join('');
document.getElementById('note').innerHTML =
  `<b>What's tracked, and what isn't.</b> APN, owner, value and situs/mailing come from each county's `
  + `measured coverage. Geometry is not tracked here (last full measure ~99.6%, 2026-06-21). `
  + `<b>Land use</b> is tracked on ${n.lu_tracked} of ${n.counties_in} counties &mdash; untracked counties show `
  + `<span class="pill na">-</span> and count as 0 in the weighted rollups (a floor, not a confirmed 0%). `
  + `<b>Owner mailing-state</b> is effectively unbuilt (${n.ms_tracked} counties) and is excluded from the headline figures. `
  + `<b>${num(n.missing_n)} of ${num(n.counties_total)} counties</b> are not yet ingested `
  + `(${n.missing_breakdown.map(([l, c]) => num(c) + ' ' + l.toLowerCase()).join(' &middot; ')}) `
  + `&mdash; the "why" per county comes from the crosswalk's recorded source method; use the County filter to list them by reason.`;

// generic sortable table
function makeTable(elId, cols, getRows, onRowClick) {
  const tbl = document.getElementById(elId);
  tbl.querySelector('thead').innerHTML = '<tr>' + cols.map(c =>
    `<th class="${c.num ? 'num' : ''}" data-k="${c.k}">${c.h}</th>`).join('') + '</tr>';
  let sortK = cols[0].sortDefault || null, dir = -1;
  function render() {
    let rows = getRows();
    if (sortK) {
      const col = cols.find(c => c.k === sortK);
      rows = rows.slice().sort((a, b) => {
        let x = a[sortK], y = b[sortK];
        if (col.num) { x = x ?? -1; y = y ?? -1; return (x - y) * dir; }
        return String(x).localeCompare(String(y)) * dir;
      });
    }
    tbl.querySelector('tbody').innerHTML = rows.map(r =>
      '<tr' + (onRowClick ? ' class="state-row" data-st="' + r.abbr + '"' : '') + '>'
      + cols.map(c => `<td class="${c.num ? 'num' : ''}">${c.cell(r)}</td>`).join('') + '</tr>').join('');
    tbl.querySelectorAll('th').forEach(th =>
      th.setAttribute('aria-sort', th.dataset.k === sortK ? (dir === 1 ? 'ascending' : 'descending') : 'none'));
    if (onRowClick) tbl.querySelectorAll('tr.state-row').forEach(tr =>
      tr.onclick = () => onRowClick(tr.dataset.st));
  }
  tbl.querySelectorAll('th').forEach(th => th.onclick = () => {
    const k = th.dataset.k;
    if (sortK === k) dir = -dir; else { sortK = k; dir = (cols.find(c => c.k === k).num ? -1 : 1); }
    render();
  });
  return render;
}

// states table
const stateCols = [
  { k: 'name', h: 'State', cell: r => esc(r.name) },
  { k: 'counties_in', h: 'Counties', num: true, cell: r => cofrac(r) },
  { k: 'missing_n', h: 'Missing', num: true, cell: r => num(r.missing_n) },
  { k: 'backlog_n', h: 'Backlog', num: true, cell: r => num(r.backlog_n) },
  { k: 'parcels', h: 'Parcels', num: true, sortDefault: true, cell: r => num(r.parcels) },
  { k: 'apn', h: 'APN', num: true, cell: r => pctCell(r.apn) },
  { k: 'owner', h: 'Owner', num: true, cell: r => pctCell(r.owner) },
  { k: 'value', h: 'Value', num: true, cell: r => pctCell(r.value) },
  { k: 'situs', h: 'Situs', num: true, cell: r => pctCell(r.situs) },
  { k: 'mail', h: 'Mailing', num: true, cell: r => pctCell(r.mail) },
  { k: 'lu', h: 'Land use', num: true, cell: r => pctCell(r.lu) },
  { k: 'pull_max', h: 'Latest pull', cell: r => r.pull_max || '<span class="muted">-</span>' },
];
const renderStates = makeTable('states', stateCols, () => M.states, st => {
  document.getElementById('stsel').value = st;
  applyCountyFilter();
  document.getElementById('counties').scrollIntoView({ behavior: 'smooth' });
});
renderStates();

// county table + filters
const countyCols = [
  { k: 'status', h: '', cell: r => r.status === 'missing'
      ? '<span class="pill na" title="not ingested">missing</span>'
      : '<span class="pill good" title="ingested">&#10003;</span>' },
  { k: 'county', h: 'County', cell: r => esc(r.county) },
  { k: 'state', h: 'State', cell: r => r.state },
  { k: 'parcels', h: 'Parcels', num: true, sortDefault: true, cell: r => r.parcels == null ? '<span class="muted">-</span>' : num(r.parcels) },
  { k: 'method', h: 'Method', cell: r => esc(r.method) || '<span class="muted">-</span>' },
  { k: 'host', h: 'Source', cell: r => r.host
      ? `<a class="host" href="${esc(r.url)}" target="_blank" rel="noopener" title="${esc(r.note)}">${esc(r.host)}</a>`
      : '<span class="muted">-</span>' },
  { k: 'reason', h: 'Reason (if missing)', cell: r => r.reason
      ? `<span title="${esc(r.note)}">${esc(r.reason)}</span>`
      : '<span class="muted">-</span>' },
  { k: 'apn', h: 'APN', num: true, cell: r => pctCell(r.apn) },
  { k: 'owner', h: 'Owner', num: true, cell: r => pctCell(r.owner) },
  { k: 'value', h: 'Value', num: true, cell: r => pctCell(r.value) },
  { k: 'situs', h: 'Situs', num: true, cell: r => pctCell(r.situs) },
  { k: 'mail', h: 'Mailing', num: true, cell: r => pctCell(r.mail) },
  { k: 'lu', h: 'Land use', num: true, cell: r => pctCell(r.lu) },
  { k: 'vintage', h: 'Vintage', cell: r => esc(r.vintage) || '<span class="muted">-</span>' },
  { k: 'pulled', h: 'Pulled', cell: r => r.pulled || '<span class="muted">-</span>' },
];
let filtered = M.counties;
const renderCounties = makeTable('counties', countyCols, () => filtered);
const stsel = document.getElementById('stsel');
stsel.innerHTML = '<option value="">All states</option>' +
  M.states.map(s => `<option value="${s.abbr}">${esc(s.name)} (${s.counties_in})</option>`).join('');
const stat = document.getElementById('stat');
function applyCountyFilter() {
  const q = document.getElementById('q').value.trim().toLowerCase();
  const st = stsel.value;
  const [svStatus, svGroup] = stat.value.split(':');   // '' | 'in' | 'missing' | 'missing:<group>'
  filtered = M.counties.filter(r =>
    (!st || r.state === st) &&
    (!svStatus || r.status === svStatus) &&
    (!svGroup || r.group === svGroup) &&
    (!q || (r.county + ' ' + r.state + ' ' + (r.host || '') + ' ' + (r.method || '') + ' ' + (r.reason || '')).toLowerCase().includes(q)));
  document.getElementById('ccount').textContent = `showing ${filtered.length.toLocaleString()} of ${M.counties.length.toLocaleString()} counties`;
  renderCounties();
}
document.getElementById('q').oninput = applyCountyFilter;
stsel.onchange = applyCountyFilter;
stat.onchange = applyCountyFilter;
document.getElementById('reset').onclick = () => {
  document.getElementById('q').value = ''; stsel.value = ''; stat.value = ''; applyCountyFilter();
};
applyCountyFilter();
</script>
</body>
</html>
"""


def write_html(model):
    # trim county records to the fields the page renders (keeps the embedded JSON lean)
    keep = ("fips", "county", "state", "parcels", "method", "host", "url", "note",
            "apn", "owner", "value", "situs", "mail", "lu", "vintage", "pulled", "status")
    counties = [{k: r.get(k) for k in keep} for r in model["counties"]]
    # append the not-yet-ingested counties as lean rows (name/state/status only) so the
    # interactive table covers the full universe and "Missing only" lists the gap
    for abbr, miss in model.get("missing_by_state", {}).items():
        for m in miss:
            counties.append({"fips": m["fips"], "county": m["county"], "state": abbr, "status": "missing",
                             "reason": m["reason"], "group": m["group"], "note": m["note"]})
    payload = {
        "national": model["national"],
        "states": model["states"],
        "counties": counties,
    }
    blob = json.dumps(payload, separators=(",", ":"), allow_nan=False)
    html = HTML_TEMPLATE.replace("__DATA__", blob)
    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, "coverage_catalog.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"  html     -> {os.path.relpath(path, ROOT)} ({os.path.getsize(path) / 1024:,.0f} KB)")


def main():
    argparse.ArgumentParser(description=__doc__,
                            formatter_class=argparse.RawDescriptionHelpFormatter).parse_args()
    if not os.path.exists(MANIFEST):
        sys.exit(f"manifest not found: {MANIFEST}")
    model = build_model()
    n = model["national"]
    print(f"Catalog: {commas(n['parcels'])} parcels, {n['jurisdictions']} jurisdictions, "
          f"{n['counties_in']} counties (data through {n['as_of']})")
    write_markdown(model)
    write_html(model)


if __name__ == "__main__":
    main()
