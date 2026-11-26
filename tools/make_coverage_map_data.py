"""Build the data file behind the county coverage map (docs/counties.js).

The map page reads one small file. This script makes that file from two inputs:

  1. the county status file that src/build_map_data.py writes
     (output/counties_status.geojson): county totals and fill rates
  2. the source manifest (data/parcels/manifest.json): each county's public
     map-service address, the name of its parcel-number field, the names of
     its fields for a few facts about the land (land use, value, size, year
     built, zoning, last sale), and the names of its owner and site-address
     fields
  3. the publication policy (config/publication_policy.json): states whose
     owner and address fields must not be passed to the map at all

One county (King, WA) is drawn from a different public layer of the same county
than the manifest names. See MAP_ONLY_SOURCE below.

The output holds county-level totals, public source addresses and field NAMES
only. It holds no parcel records, no owner names and no addresses of properties.

The map uses the source address to draw parcel outlines live from the county's
own server when a visitor zooms in. When a visitor clicks one parcel, the map
asks that server for that parcel's owner and shows the name only when it reads
as an organization. Only secure (https) ArcGIS layer addresses are passed
through, because a secure page cannot call anything else.

Usage:
  python tools/make_coverage_map_data.py output/counties_status.geojson data/parcels/manifest.json docs/counties.js
"""
import collections
import json
import pathlib
import re
import sys

POLICY_PATH = pathlib.Path(__file__).resolve().parent.parent / 'config' / 'publication_policy.json'

LAYER_QUERY = re.compile(r'^https://[^?#\s]+/(FeatureServer|MapServer)/[0-9]+/query$')
SIMPLE_FIELD = re.compile(r'^[A-Za-z_][A-Za-z0-9_.]*$')
# A parcel-number field whose name could be a person or address field is not passed to the map.
DOUBTFUL_FIELD = re.compile('own|name|addr|mail|situs|street|taxpayer', re.I)
# Facts about the land the map may ask a county's server for, besides the parcel number.
# Short key in the data file -> column name in the manifest's field map.
EXTRA_FIELDS = [
    ('lu', 'land_use'), ('lud', 'land_use_desc'), ('av', 'assessed_value'), ('lv', 'land_value'),
    ('iv', 'improvement_value'), ('ac', 'acreage'), ('yb', 'year_built'), ('sf', 'building_sqft'),
    ('zn', 'zoning'), ('sd', 'sale_date'), ('sp', 'sale_price'),
]
# A source field whose name suggests a person or an address is never passed to the map,
# whatever column the manifest files it under.
PERSON_FIELD = re.compile('owner|ownr|ownnam|own_|^own|mail|taxpayer|addr|situs|street|grantee|grantor|'
                          'buyer|seller|resident|occupant|contact|phone|email|person', re.I)
# The owner column may list several source fields ("NAME1|NAME2"). A part whose name says
# it is a mailing or care-of line is left out: the map never asks for a mailing field.
NOT_AN_OWNER_NAME = re.compile('mail|addr|care|street|city|zip|phone|email|attn|contact', re.I)
# A site-address field whose name suggests the owner's mailing address is not passed on.
NOT_A_SITE_ADDRESS = re.compile('mail|taxpayer|ownr|owner(?!ship)|care|contact|phone|email', re.I)
MAX_OWNER_FIELDS = 4

# Map-only sources. For these counties the map draws from a different public layer of the
# same county than the one named in the manifest. This changes what the map asks for when a
# visitor zooms in. It does not change the store or the manifest.
#
# King County, WA: the layer in the manifest (Property/KingCo_Parcels/MapServer/0) carries
# the parcel number and nothing else, so the map could show only that. The county publishes
# land use, land and building value, size and zoning on its own property-information layer
# on the same server (checked 2026-11-26). That layer also carries a street address and a
# property name; neither is listed here, and no owner field is listed, so none is asked for.
MAP_ONLY_SOURCE = {
    '53033': {
        'source_url': 'https://gismaps.kingcounty.gov/arcgis/rest/services/Property/KingCo_PropertyInfo/MapServer/2/query',
        'field_map': {
            'apn': 'PIN',
            'land_use': 'PREUSE_CODE',
            'land_use_desc': 'PREUSE_DESC',
            'land_value': 'APPRLNDVAL',
            'improvement_value': 'APPR_IMPR',
            'acreage': 'KCA_ACRES',
            'zoning': 'KCA_ZONING',
        },
    },
}


def clean_date(value):
    """Keep a source date only when it starts with a plausible YYYY-MM-DD."""
    value = str(value or '')
    match = re.match('20[0-9]{2}-[0-9]{2}-[0-9]{2}', value)
    if match and '2000' <= value[:4] <= '2026':
        return match.group(0)
    return ''


def category(props):
    """One of: store, viewer, none, suppressed, pending, unprobed, pr."""
    if props['fips'].startswith('72'):
        return 'pr'
    if props['ingested']:
        return 'store'
    method = props['method']
    if method == 'web-app-only':
        return 'viewer'
    if method == 'none':
        return 'none'
    if method == 'suppressed':
        return 'suppressed'
    if method == 'unprobed':
        return 'unprobed'
    return 'pending'


def live_source(entry):
    """Return (layer query address, parcel-number field) for a manifest entry, or ('', '')."""
    for key in ('source_url', 'query_url'):
        url = entry.get(key)
        if isinstance(url, str) and LAYER_QUERY.match(url):
            field = (entry.get('field_map') or {}).get('apn')
            ok = isinstance(field, str) and SIMPLE_FIELD.match(field) and not DOUBTFUL_FIELD.search(field)
            field = field if ok else ''
            return url, field
    return '', ''


def extra_fields(entry):
    """Return 'key=field;key=field' for the land facts this county's source carries, or ''."""
    field_map = entry.get('field_map') or {}
    out = []
    for key, column in EXTRA_FIELDS:
        field = field_map.get(column)
        if isinstance(field, str) and SIMPLE_FIELD.match(field) and not PERSON_FIELD.search(field):
            out.append(key + '=' + field)
    return ';'.join(out)


def excluded_columns(policy, state):
    """Columns the publication policy bars for a state."""
    rule = (policy.get('states') or {}).get(state) or policy.get('default') or {}
    return set(rule.get('exclude_fields') or [])


def owner_fields(entry, barred):
    """Return 'FIELD|FIELD' naming the source fields that hold the owner's name, or ''."""
    if 'owner' in barred:
        return ''
    raw = (entry.get('field_map') or {}).get('owner')
    if not isinstance(raw, str) or not raw.strip():
        return ''
    parts = [part.strip() for part in raw.split('|')]
    # Anything that is not a plain list of field names (a "withheld" marker, say) is skipped.
    if not all(SIMPLE_FIELD.match(part) for part in parts):
        return ''
    keep = [part for part in parts if not NOT_AN_OWNER_NAME.search(part)]
    return '|'.join(keep[:MAX_OWNER_FIELDS])


def site_field(entry, barred):
    """Return the source field that holds the parcel's own street address, or ''."""
    if 'situs_address' in barred:
        return ''
    field = (entry.get('field_map') or {}).get('situs_address')
    if isinstance(field, str) and SIMPLE_FIELD.match(field) and not NOT_A_SITE_ADDRESS.search(field):
        return field
    return ''


def main(src, manifest_path, dest):
    with open(src, encoding='utf-8') as fh:
        feats = json.load(fh)['features']
    with open(manifest_path, encoding='utf-8') as fh:
        manifest = json.load(fh)
    with open(POLICY_PATH, encoding='utf-8') as fh:
        policy = json.load(fh)

    # State abbreviation by FIPS prefix, learned from counties that carry one.
    state_by_prefix = {}
    for feat in feats:
        props = feat['properties']
        if props.get('state'):
            state_by_prefix.setdefault(props['fips'][:2], props['state'])
    state_by_prefix.setdefault('72', 'PR')

    out = []
    cats = collections.Counter()
    total = 0
    live = 0
    with_owner = with_site = barred_live = map_only = 0
    for feat in feats:
        props = feat['properties']
        cat = category(props)
        cats[cat] += 1
        row = {
            'fips': props['fips'],
            'name': props['name'],
            'st': props.get('state') or state_by_prefix.get(props['fips'][:2], ''),
            'cat': cat,
        }
        if cat == 'store':
            total += int(props['parcels'] or 0)
            row.update({
                'parcels': int(props['parcels'] or 0),
                'shapes_only': bool(props['geom_only']),
                'vintage': clean_date(props.get('vintage')),
                'retrieved': props.get('retrieved') or '',
                'host': props.get('source') or '',
                'owner': round(float(props.get('owner_cov') or 0), 3),
                'value': round(float(props.get('value_cov') or 0), 3),
                'situs': round(float(props.get('situs_cov') or 0), 3),
                'mail': round(float(props.get('mail_cov') or 0), 3),
                'landuse': round(float(props.get('landuse_cov') or 0), 3),
            })
            # A map-only source replaces the manifest entry whole, and goes through the same
            # checks. It names no owner or address field, so none is passed for that county.
            entry = MAP_ONLY_SOURCE.get(props['fips']) or manifest.get(props['fips']) or {}
            if props['fips'] in MAP_ONLY_SOURCE:
                map_only += 1
            url, field = live_source(entry)
            if url:
                live += 1
                row['q'] = url
                if field:
                    row['pn'] = field
                extras = extra_fields(entry)
                if extras:
                    row['x'] = extras
                barred = excluded_columns(policy, row['st'])
                if barred:
                    barred_live += 1
                owners = owner_fields(entry, barred)
                if owners:
                    with_owner += 1
                    row['o'] = owners
                    # The site address is shown only next to an owner, so it is passed only with one.
                    site = site_field(entry, barred)
                    if site:
                        with_site += 1
                        row['sa'] = site
        out.append({'type': 'Feature', 'properties': row, 'geometry': feat['geometry']})

    body = json.dumps({'type': 'FeatureCollection', 'features': out},
                      separators=(',', ':'), ensure_ascii=False)
    with open(dest, 'w', encoding='utf-8', newline='\n') as fh:
        fh.write('// County-level coverage status, public source addresses and field names. No parcel records, no owner names.\n')
        fh.write('window.COUNTY_STATUS = ')
        fh.write(body)
        fh.write(';\n')

    print('counties', len(out), dict(cats))
    print('parcel records in the store', f'{total:,}')
    print('counties excluding Puerto Rico', len(out) - cats['pr'])
    print('store counties with a live map-service address', live)
    print('  of those: owner field passed', with_owner, '| site-address field passed', with_site,
          '| in a state the policy bars', barred_live)
    print('  of those: drawn from a map-only source (MAP_ONLY_SOURCE)', map_only)


if __name__ == '__main__':
    if len(sys.argv) != 4:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2], sys.argv[3])
