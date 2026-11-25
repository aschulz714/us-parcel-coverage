"""Discover what fields each source offers BEYOND the ~8 attributes we standardize.

Every ingested county is an ArcGIS REST layer that publishes its own schema. For a chosen set
of sources this:
  1. reads the layer's full field list           (<layer>?f=json)
  2. samples records to measure which fields are actually POPULATED, not merely defined
     (a layer can declare YEARBUILT and have it null everywhere)
  3. drops the fields we already map (manifest field_map) -> the "available but unused" set
  4. groups field names into concepts (year_built, sale_price, land-vs-improvement value, ...)
     -- names vary wildly across counties (YR_BLT/YEARBUILT/YrBuilt), so concepts let us rank
     candidates across heterogeneous sources. The grouping is keyword-heuristic; the report
     prints example field names so every candidate can be eyeballed.

Outputs:
  docs/FIELD_CANDIDATES.md          ranked candidate attributes + per-source detail
  output/source_fields_sample.csv   one row per (county, unmapped field) for re-analysis

Scope: ingested ArcGIS sources only -- the web-app-only / missing counties have no machine
endpoint to ask. --sample favors the largest county per state (richest CAMA layers), so it
shows what is POSSIBLE; --all gives true national frequency.

Usage:
  python src/field_discovery.py --sample      # largest ingested county per state (~48)
  python src/field_discovery.py --state AL    # every ingested county in a state
  python src/field_discovery.py --fips 01097  # one county
  python src/field_discovery.py --all         # every ingested source (cached/resumable)
  python src/field_discovery.py --refresh ... # ignore the probe cache
"""
import argparse, csv, json, os, re, sys, time
from collections import Counter, defaultdict

try:
    import requests
except ImportError:
    requests = None
import urllib.request

HERE = os.path.dirname(__file__)
ROOT = os.path.dirname(HERE)
MANIFEST = os.path.join(ROOT, "data", "parcels", "manifest.json")
DOCS = os.path.join(ROOT, "docs")
OUT = os.path.join(ROOT, "output")
CACHE = os.path.join(ROOT, "data", "parcels", "_field_probe_cache.json")

OURS = ["apn", "owner", "situs_address", "mailing_address", "assessed_value",
        "land_use", "acreage", "mail_state (partial)"]

SYS_FIELDS = {"OBJECTID", "OBJECTID_1", "FID", "GLOBALID", "SHAPE", "SHAPE__AREA",
              "SHAPE__LENGTH", "SHAPEAREA", "SHAPELEN", "SE_ANNO_CAD_DATA", "CREATED_USER",
              "CREATED_DATE", "LAST_EDITED_USER", "LAST_EDITED_DATE", "EDITDATE", "GDB_GEOMATTR_DATA",
              "Shape.STArea()", "Shape.STLength()", "STArea()", "STLength()"}

# concept -> substrings (matched against squashed-uppercase name AND alias). Order matters:
# more specific concepts first (lot_size before building_sqft so LOTSQFT isn't read as bldg area).
# Heuristic grouping only -- the report shows example field names so each can be verified.
CONCEPTS = [
    ("year_built",        ["YEARBUILT", "YRBLT", "YEARBLT", "ACTYRBLT", "EFFYR", "YRBUILT", "CONSTYR", "YRCONST", "ACTUALYEAR", "EFFYEAR"]),
    ("lot_size",          ["LOTSIZE", "LOTSQFT", "LOTAREA", "PARCELAREA", "LOTDIM"]),
    ("building_sqft",     ["SQFT", "SQFEET", "BLDGAREA", "BLDGSF", "HEATEDAREA", "LIVAREA", "LIVINGAREA", "FINSQFT", "TOTLVGAREA", "GROSSAREA", "BLDGSQ", "HEATEDSQ", "TOTALAREA", "FINISHEDAREA"]),
    ("stories",           ["STORIES", "NUMSTORIES", "NOSTORIES"]),
    ("bedrooms",          ["BEDROOM", "NUMBEDS", "BEDS", "NOBED", "BEDRM", "NBEDRM"]),
    ("bathrooms",         ["BATHROOM", "NUMBATH", "BATHS", "NOBATH", "FULLBATH", "BATHRM", "NBATH"]),
    ("sale_date",         ["SALEDATE", "SALEDT", "DEEDDATE", "SALESDATE", "DATESOLD", "LASTSALEDATE", "TRANSDATE", "RECDATE", "SALEYR", "SALEYEAR"]),
    ("sale_price",        ["SALEPRICE", "SALEAMT", "SALEPRC", "SALEAMOUNT", "LASTSALE", "SALESPRICE", "CONSIDERATION", "SALEPR", "SALESAMT"]),
    ("deed_reference",    ["DEEDBOOK", "DEEDPAGE", "ORBOOK", "ORPAGE", "BOOKPAGE", "INSTRUMENT", "INSTR", "DOCNUM", "DBOOK", "DEEDREF", "RECNUM"]),
    ("land_value",        ["LANDVAL", "LANDVALUE", "LNDVAL", "ASSDLAND", "MKTLAND", "LANDASSESS", "LANDMKT", "VALLAND", "LANDAV", "LANDJUST"]),
    ("improvement_value", ["IMPVAL", "IMPROVEMENT", "BLDGVAL", "BUILDINGVAL", "STRUCTVAL", "ASSDIMP", "MKTIMP", "VALIMP", "IMPRVAL", "IMPAV", "BLDGJUST"]),
    ("market_value",      ["MARKETVAL", "MKTVAL", "JUSTVAL", "APPRAIS", "FULLVAL", "FAIRMARKET", "TOTMKT", "TRUEVAL", "CURRMKT", "TOTALMKT", "FMV"]),
    ("tax_amount",        ["TAXAMT", "TAXAMOUNT", "TOTALTAX", "NETTAX", "TAXDUE", "TAXBILL", "GROSSTAX", "ANNUALTAX", "MILLAGE", "MILLRATE", "LEVY"]),
    ("exemption",         ["EXEMPT", "HOMESTEAD", "HMSTD", "HSTD"]),
    ("zoning",            ["ZONING", "ZONECODE", "ZONEDESC", "ZONECLASS", "ZONE"]),
    ("land_use_desc",     ["LANDUSE", "USEDESC", "USECODE", "PROPCLASS", "PROPTYPE", "DORUC", "PROPERTYUSE", "CLASSDESC", "USECLASS", "PROPUSE"]),
    ("legal_description", ["LEGAL", "LGLDESC", "LGL"]),
    ("subdivision",       ["SUBDIV", "PLAT", "SUBDIVISION", "SUBDNAME"]),
    ("neighborhood",      ["NEIGHBORHOOD", "NBHD", "NEIGH"]),
    ("acreage",           ["ACRE", "ACRES", "GISACRE", "DEEDACRE", "CALCACRE", "ACREAGE", "LOTACRE"]),
    ("situs_city",        ["SITUSCITY", "PROPCITY", "SITECITY", "PHYSCITY", "LOCCITY", "SITUSCTY"]),
    ("situs_zip",         ["SITUSZIP", "PROPZIP", "SITEZIP", "PHYSZIP", "LOCZIP"]),
    ("situs_state",       ["SITUSSTATE", "PROPSTATE", "SITESTATE"]),
    ("mail_city",         ["MAILCITY", "OWNERCITY", "MAILINGCITY", "MADDRCITY", "OWNCITY"]),
    ("mail_state",        ["MAILSTATE", "OWNERSTATE", "MAILINGSTATE", "OWNSTATE"]),
    ("mail_zip",          ["MAILZIP", "OWNERZIP", "MAILINGZIP", "OWNZIP"]),
    ("secondary_owner",   ["OWNER2", "NAME2", "COOWNER", "CAREOF", "INCAREOF", "OWNERNAME2", "SECONDOWNER", "ADDLOWNER"]),
]
SYS_SQUASH = {re.sub(r"[^A-Z0-9]", "", s.upper()) for s in SYS_FIELDS}


def squash(s):
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


def classify(name):
    # match on the field NAME only -- alias text (free-form) bled fields across concepts
    # (e.g. a field named ADDRESS1 with a "Tax Bill..." alias landing in tax_amount).
    a = squash(name)
    for concept, pats in CONCEPTS:
        if any(p in a for p in pats):
            return concept
    return "other"


def http_json(url, timeout):
    headers = {"User-Agent": "Mozilla/5.0 (national-parcel-coverage field-discovery)"}
    if requests:
        r = requests.get(url, headers=headers, timeout=timeout)
        r.raise_for_status()
        return r.json()
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def probe_one(fips, e, timeout=20):
    su = e.get("source_url") or ""
    base = su.rsplit("/query", 1)[0]
    fm = e.get("field_map") or {}
    mapped = {squash(p) for v in fm.values() if v for p in str(v).split("|")}
    out = {"fips": fips, "state": (e.get("state") or "").upper(), "county": e.get("county"),
           "parcels": e.get("feature_count") or 0,
           "host": (su.split("/")[2] if "://" in su else ""),
           "n_fields": 0, "n_mapped": len(mapped), "unmapped": [], "error": None}
    try:
        meta = http_json(base + "?f=json", timeout)
        fields = meta.get("fields") or []
        out["n_fields"] = len(fields)
        fill = {}
        try:  # sample real population -- best effort; some servers reject the params
            s = http_json(base + "/query?where=1%3D1&outFields=*&resultRecordCount=25"
                                 "&returnGeometry=false&f=json", timeout)
            feats = [f.get("attributes", {}) for f in (s.get("features") or [])]
            if feats:
                for fl in fields:
                    nm = fl["name"]
                    nn = sum(1 for a in feats if a.get(nm) not in (None, "")
                             and str(a.get(nm)).strip() != "")
                    fill[nm] = round(nn / len(feats), 2)
        except Exception:
            pass
        for fl in fields:
            nm = fl["name"]
            if squash(nm) in mapped or squash(nm) in SYS_SQUASH:
                continue
            out["unmapped"].append({"name": nm, "concept": classify(nm),
                                    "type": (fl.get("type") or "").replace("esriFieldType", ""),
                                    "fill": fill.get(nm)})
    except Exception as ex:
        out["error"] = repr(ex)[:160]
    return out


def select(manifest, args):
    items = [(f, e) for f, e in manifest.items() if (e.get("source_url") or "").startswith("http")]
    if args.fips:
        return [(f, e) for f, e in items if f == args.fips]
    if args.state:
        return [(f, e) for f, e in items if (e.get("state") or "").upper() == args.state.upper()]
    if args.all:
        return items
    best = {}  # --sample: the largest-parcel ingested county per state
    for f, e in items:
        st = (e.get("state") or "").upper()
        fc = e.get("feature_count") or 0
        if st and (st not in best or fc > (best[st][1].get("feature_count") or 0)):
            best[st] = (f, e)
    return sorted(best.values(), key=lambda x: -(x[1].get("feature_count") or 0))


def aggregate(results):
    agg = defaultdict(lambda: {"sources": set(), "pop": set(), "parcels": 0, "ex": Counter()})
    for r in results:
        if r.get("error"):
            continue
        per_concept = defaultdict(list)
        for u in r["unmapped"]:
            per_concept[u["concept"]].append(u)
        for concept, us in per_concept.items():
            a = agg[concept]
            a["sources"].add(r["fips"])
            for u in us:
                a["ex"][u["name"]] += 1
            if any((u["fill"] or 0) >= 0.5 for u in us):
                a["pop"].add(r["fips"])
                a["parcels"] += r["parcels"]
    return agg


def write_report(results, args):
    # re-derive concepts (name-only) and drop any system/geometry fields that slipped through;
    # works off cached probe results so classifier tweaks need no re-probe
    for r in results:
        if r.get("unmapped"):
            r["unmapped"] = [{**u, "concept": classify(u["name"])}
                             for u in r["unmapped"] if squash(u["name"]) not in SYS_SQUASH]
    ok = [r for r in results if not r.get("error")]
    errs = [r for r in results if r.get("error")]
    agg = aggregate(ok)
    rows = sorted(((c, len(a["sources"]), len(a["pop"]), a["parcels"], a["ex"])
                   for c, a in agg.items() if c != "other"),
                  key=lambda x: (-x[1], -x[2]))
    n = len(ok)
    mode = "largest county per state" if not (args.state or args.fips or args.all) else \
           (args.state or args.fips or "all ingested sources")

    L = ["# Source Field Discovery — candidate attributes to add\n",
         f"*Probed **{n}** source layers ({mode}); {len(errs)} unreachable. For each ArcGIS source "
         "we read its full field list (`?f=json`) and sampled up to 25 records to measure real "
         "population. Fields we already standardize are excluded.*\n",
         "> **Read this as what's POSSIBLE, not exact frequency.** The default sample favors large/urban "
         "counties (richest CAMA layers). Concepts are keyword-grouped (heuristic) — example field names "
         "are shown so you can verify each. Run `--all` for true national frequency.\n",
         f"**Already standardized:** {', '.join(OURS)}\n",
         "## Candidate attributes (ranked)\n",
         f"*Sources = of {n} probed that expose a matching field. Populated = of those, how many had it "
         "filled in >=50% of sampled records. Reach = parcels covered by the populated sources.*\n",
         "| Candidate concept | Sources | Populated | Parcel reach | Example source fields |",
         "|-------------------|--------:|----------:|-------------:|-----------------------|"]
    for concept, nsrc, npop, parcels, ex in rows:
        examples = ", ".join(f"`{name}`" for name, _ in ex.most_common(4))
        L.append(f"| {concept} | {nsrc} / {n} | {npop} | {parcels:,} | {examples} |")
    other = agg.get("other")
    if other:
        L.append(f"\n*Plus {len(other['sources'])} sources with uncategorized extra fields "
                 f"(e.g. {', '.join('`'+x+'`' for x, _ in other['ex'].most_common(6))}).*")

    L.append("\n## Per-source detail\n")
    for r in sorted(ok, key=lambda r: (r["state"], r["county"] or "")):
        avail = ", ".join(f"{u['name']}" + (f" [{u['concept']}]" if u["concept"] != "other" else "")
                          for u in r["unmapped"][:40])
        L.append(f"- **{r['state']} {r['county']}** ({r['host']}) — {r['n_fields']} fields, "
                 f"{r['n_mapped']} mapped, **{len(r['unmapped'])} unmapped**: {avail or '(none)'}")
    if errs:
        L.append("\n## Unreachable at probe time\n")
        for r in errs:
            L.append(f"- {r['state']} {r['county']} ({r['host']}): {r['error']}")

    os.makedirs(DOCS, exist_ok=True)
    path = os.path.join(DOCS, "FIELD_CANDIDATES.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print(f"  report -> {os.path.relpath(path, ROOT)} ({os.path.getsize(path)/1024:,.0f} KB)")

    os.makedirs(OUT, exist_ok=True)
    csv_path = os.path.join(OUT, "source_fields_sample.csv")
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["state", "county", "fips", "parcels", "host", "field", "type", "concept", "fill"])
        for r in ok:
            for u in r["unmapped"]:
                w.writerow([r["state"], r["county"], r["fips"], r["parcels"], r["host"],
                            u["name"], u["type"], u["concept"], u["fill"]])
    print(f"  csv    -> {os.path.relpath(csv_path, ROOT)}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--sample", action="store_true", help="largest ingested county per state (default)")
    g.add_argument("--state")
    g.add_argument("--fips")
    g.add_argument("--all", action="store_true")
    ap.add_argument("--refresh", action="store_true", help="ignore the probe cache")
    ap.add_argument("--timeout", type=float, default=20)
    args = ap.parse_args()

    with open(MANIFEST, encoding="utf-8") as f:
        manifest = dict(json.load(f))
    targets = select(manifest, args)
    if not targets:
        sys.exit("no matching ingested sources")
    cache = {} if args.refresh else (json.load(open(CACHE)) if os.path.exists(CACHE) else {})

    print(f"Probing {len(targets)} source layers...")
    results = []
    for i, (fips, e) in enumerate(targets, 1):
        if fips in cache and not args.refresh:
            results.append(cache[fips])
            continue
        r = probe_one(fips, e, args.timeout)
        cache[fips] = r
        results.append(r)
        flag = f"ERR {r['error'][:50]}" if r["error"] else f"{len(r['unmapped'])} unmapped of {r['n_fields']}"
        print(f"  [{i}/{len(targets)}] {r['state']} {r['county']}: {flag}", flush=True)
        json.dump(cache, open(CACHE, "w"))  # checkpoint every probe (resumable)
        time.sleep(0.2)

    write_report(results, args)
    print("done.")


if __name__ == "__main__":
    main()
