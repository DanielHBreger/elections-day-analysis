"""Build data/map.json: the shapes behind the site's maps.

Usage: python build_map.py ACCID_MUNI.shp ne_10m_admin_0_countries.shp ne_10m_lakes.shp shoreline.geojson
(needs pyshp, shapely 2.1+ and pyproj, for building only)

- Towns: the Ministry of Transport's "road accidents by municipal authority" layer on data.gov.il
  (https://data.gov.il/dataset/accidents_municipal, ACCIDENTS_MUNICIPAL_SHP), which has one polygon per locality, keyed by
  the Central Bureau of Statistics code the elections data uses. A regional council's land is split between its
  kibbutzim, moshavim and other villages, so every town has a shape. It has the West Bank as one polygon, so towns there,
  and the Bedouin tribes (which have no land of their own), stay dots on the map.
- Regional council and district outlines: the same polygons, merged.
- Israel's land, the West Bank and the Sea of Galilee: the same polygons too, so the coast and borders are one line.
- The Mediterranean coast: the Survey of Israel's shoreline (the 0.75 m line in "coastal environment protection lines",
  https://data.gov.il/dataset/kavi-hof, CC BY), which cuts off the sea some towns' areas take in (Ashdod's port,
  Hadera's power station). It leaves out the ports, which are bridged with straight lines. Its layer קו_חוף_075 comes
  as a .gdb; convert it once with `ogr2ogr -f GeoJSON shoreline.geojson X.gdb קו_חוף_075`.
- Background: Natural Earth's countries and lakes (https://www.naturalearthdata.com, public domain), for the neighbours,
  Gaza and the Dead Sea. Its 1:10m lines are up to 2 km from the municipal layer's (which agrees with the bureau's town
  coordinates), so each neighbour is cut at the municipal layer's edge, and the gap between the two edges goes to
  whichever neighbour is nearest, up to REACH away.

Everything is in Israeli grid (ITM) metres, like data/towns.json, quantised to Q metres. Each ring is a flat list of
x, y pairs, the first pair from the view's south-west corner and the rest as steps from the previous point."""
import json
import sys
from collections import defaultdict
from pathlib import Path

import shapefile
import shapely
from pyproj import Transformer
from shapely.geometry import box, shape

Q = 50                          # metres per unit in the output
TOLERANCE = 150                 # simplification: about a third of a pixel at the site's map width
VIEW = (118000, 368000, 292000, 812000)  # x0, y0, x1, y1: Israel with a margin of sea and neighbouring land
NO_TOWN = {'ללא שיפוט', ''}     # areas outside any authority
KINNERET = 'ללא שיפוט - אזור הכנרת'  # the lake itself, outside any authority
REACH = 4000                    # metres a neighbour may stretch to meet Israel's edge (the gaps are up to about 3 km)
STEP = 250                      # metres the neighbours grow at a time, taking turns, so each gets the gap nearest to it


def mediterranean(shoreline_path):
    """The sea west of the shoreline: its pieces chained from south to north (bridging the ports), closed far out at sea."""
    parts = [line for f in json.loads(Path(shoreline_path).read_text('utf-8'))['features']
             for line in shapely.get_parts(shapely.line_merge(shape(f['geometry'])))]
    parts = [p for p in parts if p.bounds[1] > 500000]  # the Eilat coast needs no cut
    start = min(parts, key=lambda p: min(p.coords[0][1], p.coords[-1][1]))
    chain = list(start.coords) if start.coords[0][1] < start.coords[-1][1] else list(start.coords)[::-1]
    parts.remove(start)
    while parts:  # the piece with the end nearest to where the chain stops, turned to start there
        end = shapely.Point(chain[-1])
        p = min(parts, key=lambda p: min(end.distance(shapely.Point(p.coords[0])), end.distance(shapely.Point(p.coords[-1]))))
        parts.remove(p)
        chain += list(p.coords) if end.distance(shapely.Point(p.coords[0])) <= end.distance(shapely.Point(p.coords[-1])) else list(p.coords)[::-1]
    sea = shapely.Polygon(chain + [(0, chain[-1][1]), (0, chain[0][1])])
    assert sea.is_valid and sea.area > 1e9, 'the shoreline did not chain into one coast'
    return sea


def rings(geom):
    """A polygon or multipolygon as encoded rings, dropping slivers too small to see."""
    out = []
    for poly in getattr(geom, 'geoms', [geom]):
        if poly.geom_type != 'Polygon' or poly.area < (2 * Q) ** 2:
            continue
        for ring in [poly.exterior, *poly.interiors]:
            pts, flat, px, py = [(round((x - VIEW[0]) / Q), round((y - VIEW[1]) / Q)) for x, y in ring.coords[:-1]], [], 0, 0
            for x, y in pts:
                if (x, y) != (px, py) or not flat:
                    flat += [x - px, y - py]
                    px, py = x, y
            if len(flat) >= 6:
                out.append(flat)
    return out


def main(muni_path, countries_path, lakes_path, shoreline_path):
    r = shapefile.Reader(muni_path, encoding='utf-8')
    recs = r.records()
    sea = mediterranean(shoreline_path)
    geoms = [shapely.make_valid(shape(s.__geo_interface__)).difference(sea) for s in r.shapes()]
    # Simplified together, so neighbours keep sharing their edges and no gaps open between them.
    geoms = list(shapely.coverage_simplify(geoms, TOLERANCE))
    towns, councils, districts = defaultdict(list), defaultdict(list), defaultdict(list)
    for rec, g in zip(recs, geoms):
        city = rec['CITY'] or ''
        if rec['MUNITYPE'] in NO_TOWN or city.startswith('שטח כללי') or rec['MUNICIPAL'] == 'יהודה ושומרון':
            pass  # open land, and the West Bank as a whole: background, not towns
        else:
            towns[str(rec['CITYCODE'])].append(g)
        if rec['MUNITYPE'] == 'מועצה אזורית':
            councils[rec['MUNICODE']].append(g)
        if rec['DISTRICT'] and rec['MUNICIPAL'] != 'יהודה ושומרון':
            districts[rec['DISTRICT']].append(g)
    merge = lambda parts: shapely.union_all(parts).buffer(0)

    to_itm = Transformer.from_crs('EPSG:4326', 'EPSG:2039', always_xy=True).transform
    view = box(*VIEW)
    def background(path, keep):
        out = []
        for sr in shapefile.Reader(path).iterShapeRecords():
            d = sr.record.as_dict()
            if not keep(d):
                continue
            g = shapely.transform(shape(sr.shape.__geo_interface__).intersection(box(33.0, 28.8, 37.2, 34.2)), to_itm, interleaved=False)
            g = g.intersection(view).simplify(TOLERANCE)
            if not g.is_empty:
                out.append((d, g))
        return out
    countries = background(countries_path, lambda d: d['ADM0_A3'] in ('ISR', 'PSX', 'LBN', 'SYR', 'JOR', 'EGY', 'SAU'))
    ne_lakes = shapely.union_all([g for _, g in background(lakes_path, lambda d: d.get('name') in ('Sea of Galilee', 'Dead Sea'))])

    # Fit Natural Earth around the municipal layer: home is all of the layer; Gaza (the part of Natural Earth's PSX the
    # layer lacks) and each neighbour take what they had outside it, plus the part nearest to them of the gap between their
    # old edge and the layer's, where Natural Earth had Israel or PSX. Natural Earth's land beyond the shoreline stays sea.
    home = shapely.union_all(geoms)
    ours = shapely.union_all([g for d, g in countries if d['ADM0_A3'] in ('ISR', 'PSX')])
    gap = ours.difference(home).difference(ne_lakes).difference(sea)
    gaza_city = shapely.Point(to_itm(34.45, 31.50))
    psx = shapely.union_all([g for d, g in countries if d['ADM0_A3'] == 'PSX'])
    gaza = next(p for p in shapely.get_parts(psx.difference(home)) if p.contains(gaza_city))  # the rest of PSX is slivers
    others = [(True, gaza)] + [(False, g.difference(home)) for d, g in countries if d['ADM0_A3'] not in ('ISR', 'PSX')]
    claimed, grown = shapely.union_all([home] + [g for _, g in others]), [g for _, g in others]
    for reach in range(STEP, REACH + 1, STEP):
        for i, (_, g) in enumerate(others):
            more = g.buffer(reach).intersection(gap).difference(claimed)
            grown[i], claimed = grown[i].union(more), claimed.union(more)
    # buffer(0): a difference can leave stray lines and points; keep only the area.
    land = [(True, home)] + [(is_home, g.difference(ne_lakes).buffer(0)) for (is_home, _), g in zip(others, grown)]
    kinneret = [g for rec, g in zip(recs, geoms) if rec['MUNICIPAL'] == KINNERET]
    lakes = [merge(kinneret), ne_lakes.difference(home)]

    data = {
        'q': Q, 'view': VIEW,
        'towns': {code: rings(merge(parts)) for code, parts in sorted(towns.items())},
        'councils': [rings(merge(parts)) for parts in councils.values()],
        'districts': [rings(merge(parts)) for parts in districts.values()],
        # Israel and the Palestinian territories are "home" land, the others neighbouring land.
        'land': [{'home': is_home, 'rings': rings(g)} for is_home, g in land],
        'lakes': [rings(g) for g in lakes],
    }
    out = Path(__file__).resolve().parent / 'data' / 'map.json'
    out.write_text(json.dumps(data, separators=(',', ':')), 'utf-8')
    print(f"{len(data['towns'])} towns, {len(data['councils'])} regional councils, {len(data['districts'])} districts: "
          f"{out.stat().st_size // 1024} KB written to {out}")


if __name__ == '__main__':
    main(*sys.argv[1:5])
