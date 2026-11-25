"""Build the data file behind the county coverage map (docs/counties.js).

The map page reads one small file. This script makes that file from two inputs:

  1. the county status file that src/build_map_data.py writes
     (output/counties_status.geojson): county totals and fill rates
  2. the source manifest (data/parcels/manifest.json): each county's public
     map-service address, the name of its parcel-number field, and the names
     of its fields for a few facts about the land (land use, value, size,
     year built, zoning, last sale)

The output holds county-level totals and public source addresses only. It holds
no parcel records, no owner names and no addresses of properties.

The map uses the source address to draw parcel outlines live from the county's
own server when a visitor zooms in. Only secure (https) ArcGIS layer addresses
are passed through, because a secure page cannot call anything else.

Usage:
  python tools/make_coverage_map_data.py output/counties_status.geojson data/parcels/manifest.json docs/counties.js
"""
import collections
import json
import re
import sys

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


def main(src, manifest_path, dest):
    with open(src, encoding='utf-8') as fh:
        feats = json.load(fh)['features']
    with open(manifest_path, encoding='utf-8') as fh:
        manifest = json.load(fh)

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
            url, field = live_source(manifest.get(props['fips']) or {})
            if url:
                live += 1
                row['q'] = url
                if field:
                    row['pn'] = field
                extras = extra_fields(manifest.get(props['fips']) or {})
                if extras:
                    row['x'] = extras
        out.append({'type': 'Feature', 'properties': row, 'geometry': feat['geometry']})

    body = json.dumps({'type': 'FeatureCollection', 'features': out},
                      separators=(',', ':'), ensure_ascii=False)
    with open(dest, 'w', encoding='utf-8', newline='\n') as fh:
        fh.write('// County-level coverage status and public source addresses. No parcel records, no owner names.\n')
        fh.write('window.COUNTY_STATUS = ')
        fh.write(body)
        fh.write(';\n')

    print('counties', len(out), dict(cats))
    print('parcel records in the store', f'{total:,}')
    print('counties excluding Puerto Rico', len(out) - cats['pr'])
    print('store counties with a live map-service address', live)


if __name__ == '__main__':
    if len(sys.argv) != 4:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2], sys.argv[3])
