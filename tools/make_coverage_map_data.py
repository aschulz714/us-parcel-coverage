"""Build the data file behind the county coverage map (docs/counties.js).

The map page reads one small file of county totals. This script makes that file
from the county status file that src/build_map_data.py writes
(output/counties_status.geojson). It keeps county-level totals only: no parcel
records, no owner names, no addresses.

Usage:
  python tools/make_coverage_map_data.py output/counties_status.geojson docs/counties.js
"""
import collections
import json
import re
import sys


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


def main(src, dest):
    with open(src, encoding='utf-8') as fh:
        feats = json.load(fh)['features']

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
        out.append({'type': 'Feature', 'properties': row, 'geometry': feat['geometry']})

    body = json.dumps({'type': 'FeatureCollection', 'features': out},
                      separators=(',', ':'), ensure_ascii=False)
    with open(dest, 'w', encoding='utf-8', newline='\n') as fh:
        fh.write('// County-level coverage status. Totals only: no parcel records, no owner names.\n')
        fh.write('window.COUNTY_STATUS = ')
        fh.write(body)
        fh.write(';\n')

    print('counties', len(out), dict(cats))
    print('parcel records in the store', f'{total:,}')
    print('counties excluding Puerto Rico', len(out) - cats['pr'])


if __name__ == '__main__':
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2])
