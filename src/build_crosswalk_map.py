"""
Build the national CROSSWALK COVERAGE map: output/crosswalk_coverage_map.html

A US county choropleth of the owner-resolution crosswalk (src/county_methods.csv): every
county we've crosswalked, colored by HOW its owner is reachable. Single self-contained HTML
(Leaflet from CDN, geometry + crosswalk embedded). Distinct from build_map.py, which is the
WA-only data-quality grading map.

Geometry: data/us_counties.geojson (Census cartographic county polygons, FIPS-keyed `id`).
Rebuild: python src/build_crosswalk_map.py
"""
import os, csv, json
from collections import Counter

HERE = os.path.dirname(__file__)
ROOT = os.path.dirname(HERE)
XW = os.path.join(HERE, "county_methods.csv")
GEO = os.path.join(ROOT, "data", "us_counties.geojson")
OUT = os.path.join(ROOT, "output", "crosswalk_coverage_map.html")

# method -> (category, human label). Categories drive the color + legend.
CAT = {
    "arcgis":          ("resolves", "Resolves directly — live point→owner"),
    "arcgis-join":     ("twostep",  "2-step — owner in a joinable table"),
    "arcgis-centroid": ("twostep",  "2-step — statewide centroid layer"),
    "wms":             ("twostep",  "2-step — WMS GetFeatureInfo"),
    "wfs":             ("twostep",  "2-step — WFS GetFeature (GeoServer)"),
    "web-app-only":    ("offrest",  "Owner public, but only via a web app"),
    "bulk-download":   ("offrest",  "Owner only in a bulk download"),
    "deed-records":    ("offrest",  "Owner via deed / grantee records"),
    "suppressed":      ("blocked",  "Owner withheld from public REST"),
    "none":            ("nosource", "No public programmatic source"),
    "unprobed":        ("unprobed", "Not yet probed"),
}
COLOR = {"resolves": "#1a7d32", "twostep": "#00897b", "offrest": "#f9a825",
         "blocked": "#e53935", "nosource": "#37474f", "unprobed": "#bdbdbd"}
CATLABEL = {"resolves": "Resolves directly", "twostep": "2-step (join / WMS / centroid)",
            "offrest": "Owner public, off-REST", "blocked": "Owner blocked (suppressed)",
            "nosource": "No public source", "unprobed": "Not yet probed"}

xw = {}
with open(XW, encoding="utf-8") as f:
    for r in csv.DictReader(f):
        xw[r["fips"]] = r

def simp_ring(ring):
    out = []
    for x, y in ring:
        p = [round(x, 3), round(y, 3)]
        if not out or out[-1] != p:
            out.append(p)
    return out if len(out) >= 4 else [[round(x, 3), round(y, 3)] for x, y in ring]

def simp_geom(g):
    t = g["type"]; c = g["coordinates"]
    if t == "Polygon":
        return {"type": t, "coordinates": [simp_ring(r) for r in c]}
    if t == "MultiPolygon":
        return {"type": t, "coordinates": [[simp_ring(r) for r in poly] for poly in c]}
    return g

raw = json.load(open(GEO, encoding="utf-8"))
geom_by_fips = {ft.get("id"): ft["geometry"] for ft in raw["features"]}

feats, missing = [], []
for fips, r in xw.items():
    g = geom_by_fips.get(fips)
    if not g:
        missing.append(fips); continue
    cat = CAT.get(r["method"], ("unprobed", r["method"]))[0]
    feats.append({"type": "Feature", "geometry": simp_geom(g), "properties": {
        "fips": fips, "county": r["county"], "state": r["state"], "method": r["method"],
        "cat": cat, "mlabel": CAT.get(r["method"], ("", r["method"]))[1],
        "owner": r.get("owner_fields", ""), "url": r.get("query_url", ""),
        "pq": r.get("point_query", ""),
    }})
gj = {"type": "FeatureCollection", "features": feats}

catcount = Counter(ft["properties"]["cat"] for ft in feats)
statecount = Counter(ft["properties"]["state"] for ft in feats)
order = ["resolves", "twostep", "offrest", "blocked", "nosource", "unprobed"]
counts = {c: catcount.get(c, 0) for c in order}

HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Parcel Owner-Resolution Crosswalk — Coverage</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  html,body{margin:0;height:100%;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif}
  #map{position:absolute;top:0;bottom:0;left:0;right:0}
  .panel{position:absolute;z-index:1000;background:#fff;padding:10px 12px;border-radius:8px;
         box-shadow:0 1px 6px rgba(0,0,0,.3);font-size:13px}
  #title{top:10px;left:10px;max-width:360px}
  #title h1{font-size:15px;margin:0 0 4px}
  #title p{margin:3px 0;color:#444;line-height:1.35}
  #legend{bottom:18px;left:10px}
  #legend .row{display:flex;align-items:center;margin:2px 0;cursor:default}
  #legend i{width:16px;height:16px;margin-right:8px;border:1px solid #999;border-radius:3px}
  .leaflet-popup-content{font-size:12.5px;line-height:1.4;max-width:300px}
  .leaflet-popup-content h3{margin:0 0 4px;font-size:14px}
  .kv{margin:2px 0}.kv b{display:inline-block;min-width:62px;color:#555;font-weight:600}
  a.ep{word-break:break-all;color:#1558b0}
  .tot{margin-top:6px;border-top:1px solid #eee;padding-top:5px;color:#333}
</style></head><body>
<div id="map"></div>
<div id="title" class="panel">
  <h1>Parcel Owner-Resolution Crosswalk</h1>
  <p>Every county we can route from a <b>location → parcel → owner name</b>, colored by HOW the
  owner is reachable. Each
  county was verified against its live source — none guessed.</p>
  <div class="tot"><b>__NC__ counties</b> across __NS__ states · <b>__NR__ resolve directly</b></div>
</div>
<div id="legend" class="panel"></div>
<script>
var DATA = __GEOJSON__, COUNTS = __COUNTS__;
var COL = __COLOR__, CATLABEL = __CATLABEL__, ORDER = __ORDER__;
var map = L.map("map").setView([39.5, -111.5], 5);
L.tileLayer("https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png",
  {attribution:"&copy; OpenStreetMap, &copy; CARTO", subdomains:"abcd", maxZoom:19}).addTo(map);
function style(f){ return {fillColor: COL[f.properties.cat]||"#999", weight:.6, color:"#777",
                          fillOpacity:.72, opacity:1}; }
function popup(p){
  var h = '<h3>'+p.county+' County, '+p.state+'</h3>';
  h += '<div class="kv"><b>Owner</b> <span style="color:'+(COL[p.cat])+';font-weight:700">'+p.mlabel+'</span></div>';
  h += '<div class="kv"><b>Method</b> '+p.method+(p.pq==="Y"?" (point-queryable)":"")+'</div>';
  if(p.owner) h += '<div class="kv"><b>Field</b> '+p.owner+'</div>';
  if(p.url) h += '<div class="kv"><b>Source</b><br><a class="ep" href="'+p.url+'" target="_blank">'+p.url+'</a></div>';
  return h;
}
var layer = L.geoJSON(DATA, {style:style, onEachFeature:function(f,l){
  l.bindPopup(popup(f.properties));
  l.bindTooltip(f.properties.county+", "+f.properties.state,{sticky:true});
  l.on("mouseover",function(){ l.setStyle({weight:2,color:"#000",fillOpacity:.88}); });
  l.on("mouseout",function(){ layer.resetStyle(l); });
}}).addTo(map);
var html = '<b>Owner reachability</b>';
ORDER.forEach(function(c){ if(COUNTS[c]>0)
  html += '<div class="row"><i style="background:'+COL[c]+'"></i>'+CATLABEL[c]+' <small>&nbsp;('+COUNTS[c]+')</small></div>'; });
document.getElementById("legend").innerHTML = html;
</script></body></html>"""

html = (HTML.replace("__GEOJSON__", json.dumps(gj))
            .replace("__COUNTS__", json.dumps(counts))
            .replace("__COLOR__", json.dumps(COLOR))
            .replace("__CATLABEL__", json.dumps(CATLABEL))
            .replace("__ORDER__", json.dumps(order))
            .replace("__NC__", str(len(feats)))
            .replace("__NS__", str(len(statecount)))
            .replace("__NR__", str(counts["resolves"])))
os.makedirs(os.path.dirname(OUT), exist_ok=True)
with open(OUT, "w", encoding="utf-8") as f:
    f.write(html)
print(f"wrote map ({round(os.path.getsize(OUT)/1024)} KB) -> {OUT}")
print(f"  {len(feats)} counties mapped across {len(statecount)} states; {len(missing)} missing geometry {missing[:10]}")
print("  by category:", dict(counts))
print("  by state:", dict(sorted(statecount.items())))
