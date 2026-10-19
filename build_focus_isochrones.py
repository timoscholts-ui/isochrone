# -*- coding: utf-8 -*-
"""
Stap 1 — Focuspunten-grid + reistijdisochronen via open-source OSMnx (geen AGOL credits).

Voor de 4 nieuwe gebieden (Houten, Zeist, Utrechtse Heuvelrug, Wijk Oost) bouwen
we een grid van focuspunten en lossen we per modaliteit de isochronen op
(5/10/15/20 min) met een lokaal OSM-netwerk + Dijkstra:

  auto  : OSM drive-netwerk, snelheid uit OSM maxspeed.
  fiets : OSM walk-netwerk, 15 km/h (zelfde methode als Bunnik's walk×3).
  lopen : OSM walk-netwerk, 5 km/h.

Per focuspunt: single Dijkstra op het drive-netwerk én op het walk-netwerk; de
fiets-afstanden worden afgeleid door de looptijd met 3 te schalen. Bereikbare
nodes → gebufferde netwerk-unie (edge/node-buffer, geen convex hull) → polygoon.

Output per gebied (EPSG:28992):
  - <slug>_focus_isochrones.geojson
  - <slug>_focus_points.json
"""

import os
import sys
import json
import re
import time
import gc

import networkx as nx
import numpy as np
import osmnx as ox
from osmnx.distance import nearest_nodes
from shapely.geometry import Point, LineString, MultiLineString, Polygon, MultiPolygon
from shapely.ops import unary_union
from pyproj import Transformer

import isochrone_areas as areas

BREAKS = [5, 10, 15, 20]        # minuten
WALK_SPEED_KMH = 5.0
BIKE_SPEED_KMH = 15.0
BIKE_SPEED_RATIO = BIKE_SPEED_KMH / WALK_SPEED_KMH   # 3.0
EDGE_BUFF = 300.0               # meter, breedte van de netwerk-buffer (min. 300 m voor minder ruis)
SIMPLIFY_TOL = 30.0             # meter
# Buffer rondom het gebied voor het netwerk (meter). Drive reikt ~15 km (auto)
# en de fiets-isochroon ~5 km (20 min à 15 km/h over het walk-netwerk); de KPI
# telt supermarkten nu cross-regio, dus de buffers moeten de volledige
# isochroon-reikwijdte dekken. Env-overrides blijven beschikbaar voor snelle
# testruns (tijd en geheugen schalen met het kwadraat van de buffer).
DRIVE_MARGIN = float(os.environ.get("ISO_DRIVE_MARGIN", areas.DRIVE_MARGIN))
WALK_MARGIN = float(os.environ.get("ISO_WALK_MARGIN", areas.WALK_MARGIN))

# Overpass is wispelturig — probeer meerdere mirrors bij een timeout.
# NB: osmnx plakt zelf "/interpreter" achter deze URL's — dus geen /interpreter
# opnemen. (osm.ch bleek een lege database te serveren; alleen api.de werkt hier.)
_OVERPASS_MIRRORS = [
    "https://overpass-api.de/api",
    "https://overpass.kumi.systems/api",
]

_TO_WGS84 = Transformer.from_crs(28992, 4326, always_xy=True)
_TO_RD = Transformer.from_crs(4326, 28992, always_xy=True)


# ---------------------------------------------------------------------------
# Netwerk
# ---------------------------------------------------------------------------
def _wgs84_bbox(geom_rd):
    """RD-geometrie -> WGS84-bbox als (left, bottom, right, top) = (west, south, east, north)."""
    minx, miny, maxx, maxy = geom_rd.bounds
    lons, lats = [], []
    for x, y in [(minx, miny), (minx, maxy), (maxx, miny), (maxx, maxy)]:
        lon, lat = _TO_WGS84.transform(x, y)
        lons.append(lon)
        lats.append(lat)
    return (min(lons), min(lats), max(lons), max(lats))


def _find_pbf():
    """Lokaal Geofabrik .pbf-bestand (fallback voor Overpass) als het bestaat."""
    p = os.environ.get("ISO_PBF")
    if p and os.path.exists(p):
        return p
    cand = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "netherlands-latest.osm.pbf")
    return cand if os.path.exists(cand) else None


def load_networks(boundary_rd, drive_margin, walk_margin):
    """(drive_G, walk_G) — geprojecteerd (RD), drive met `speed_kph`.

    Gebruikt bij voorkeur een lokaal Geofabrik .pbf (betrouwbaar, geen credits,
    éénmalig inlezen voor beide netwerken); anders de Overpass API. De edge
    `length` blijft de great-circle-afstand in meters (niet herberekenen na
    projectie: `add_edge_lengths` verwacht lat/lon).
    """
    pbf = _find_pbf()
    if pbf:
        from pyrosm import OSM
        # Drive-netwerk: volledige drive-marge (15 km). Walk/fiets-netwerk: alleen
        # de walk-marge (5 km) nodig — los inlezen scheelt ~2 GB aan walk-graaf
        # (oppervlakte schaalt kwadratisch met de marge).
        boundary = boundary_rd.buffer(drive_margin)
        left, bottom, right, top = _wgs84_bbox(boundary)
        osm = OSM(pbf, bounding_box=[left, bottom, right, top])
        drive_G = _osm_network(osm, "drive")
        del osm
        gc.collect()
        boundary = boundary_rd.buffer(walk_margin)
        left, bottom, right, top = _wgs84_bbox(boundary)
        osm = OSM(pbf, bounding_box=[left, bottom, right, top])
        walk_G = _osm_network(osm, "walk")
        del osm
        gc.collect()
    else:
        drive_G = _download_network(boundary_rd, "drive", drive_margin)
        walk_G = _download_network(boundary_rd, "walk", walk_margin)
    drive_G = ox.truncate.largest_component(drive_G, strongly=False)
    walk_G = ox.truncate.largest_component(walk_G, strongly=False)
    return drive_G, walk_G


def _download_network(boundary_rd, network_type, margin):
    """Overpass-download + projectie (+ speed_kph bij drive)."""
    boundary = boundary_rd.buffer(margin)
    left, bottom, right, top = _wgs84_bbox(boundary)
    G = _download_with_retry(left, bottom, right, top, network_type)
    G = ox.project_graph(G, to_crs="EPSG:28992")
    if network_type == "drive":
        G = ox.add_edge_speeds(G, fallback=50.0)
    return G


def _download_with_retry(left, bottom, right, top, network_type, attempts=4):
    last_err = None
    for i in range(attempts):
        ox.settings.overpass_url = _OVERPASS_MIRRORS[i % len(_OVERPASS_MIRRORS)]
        try:
            return ox.graph_from_bbox((left, bottom, right, top), network_type=network_type)
        except Exception as e:  # noqa: BLE001 — netwerk/overpass is onbetrouwbaar
            last_err = e
            print(f"    overpass-poging {i+1}/{attempts} mislukt "
                  f"({ox.settings.overpass_url}): {type(e).__name__}")
            time.sleep(5)
    raise last_err


def _walk_cycle_filter():
    """Walking filter + cycleway, so fietsbruggen/-tunnels are kept.

    pyrosm's ``walking_filter()`` excludes ``highway=cycleway``; dropping that
    exclusion keeps dedicated foot/bike bridges and underpasses. Its ``access``
    rule is an ``AccessRule`` object, which ``get_network``'s custom_filter
    validation rejects (it only accepts lists), so it is re-expressed as the
    plain ``foot`` exclusion below.

    ``access`` and the ``sidewalk*`` rules are dropped entirely: private-but-
    walkable bridges/passages (``access=no/private`` + ``foot=yes``) and roads
    whose sidewalk is mapped as a separate way must stay in the graph, otherwise
    Houten & vergelijkbare gebieden blijven onbereikbaar voor fiets/lopen over
    bruggen en snelwegpassages. Minor over-inclusion: ways with ``access=no`` and
    no ``foot`` tag become walkable (rare, harmless for routing).
    """
    from pyrosm.config.osm_filters import walking_filter, MultiValue
    f = walking_filter()
    f["highway"] = MultiValue([v for v in f["highway"] if v != "cycleway"])
    f["foot"] = MultiValue(["no", "private"])
    for key in ("access", "sidewalk", "sidewalk:both",
                "sidewalk:left", "sidewalk:right"):
        f.pop(key, None)
    return f


def _osm_network(osm, network_type):
    """Eén netwerk uit een (al ingelezen) pyrosm OSM-object -> geprojecteerde MultiDiGraph."""
    if network_type == "walk":
        nodes, edges = osm.get_network(network_type="walking", nodes=True,
                                       custom_filter=_walk_cycle_filter())
    else:
        nodes, edges = osm.get_network(network_type="driving", nodes=True)

    G = nx.MultiDiGraph()
    G.graph["crs"] = "EPSG:28992"

    # Vectorieel projecteren (geen iterrows — die is veel te traag op 10k+ rijen).
    lon = nodes["lon"].to_numpy(dtype=float)
    lat = nodes["lat"].to_numpy(dtype=float)
    x, y = _TO_RD.transform(lon, lat)
    nids = nodes["id"].to_numpy(dtype=np.int64)
    G.add_nodes_from((int(i), {"x": float(xx), "y": float(yy)})
                     for i, xx, yy in zip(nids, x, y))

    u = edges["u"].to_numpy(dtype=np.int64)
    v = edges["v"].to_numpy(dtype=np.int64)
    length = edges["length"].to_numpy(dtype=float)
    oneway = edges["oneway"].tolist()
    if network_type == "drive":
        maxspeed = edges["maxspeed"].tolist()
        highway = edges["highway"].tolist()

    edge_tuples = []
    for k in range(len(u)):
        attrs = {"length": float(length[k])}
        if network_type == "drive":
            attrs["highway"] = highway[k]
            attrs["maxspeed"] = maxspeed[k]
            attrs["speed_kph"] = _parse_maxspeed(maxspeed[k])
        edge_tuples.append((int(u[k]), int(v[k]), attrs))
        ow = str(oneway[k] or "").lower()
        if ow not in ("yes", "true", "1"):
            edge_tuples.append((int(v[k]), int(u[k]), attrs))
    G.add_edges_from(edge_tuples)
    n0, e0 = G.number_of_nodes(), G.number_of_edges()

    # Vereenvoudigen: pyrosm geeft het onversimpelde netwerk (elk wegsegment);
    # osmnx' simplify_graph verwijdert degree-2-knopen en verkleint de graaf
    # ~10×, wat Dijkstra én bufferen flink versnelt.
    try:
        G = ox.simplify_graph(G)
    except Exception as e:  # noqa: BLE001 — fallback op onversimpelde graaf
        print(f"    simplify_graph mislukt ({type(e).__name__}): {e}; "
              f"verder met onversimpelde graaf")
    print(f"    [{network_type}] graaf {n0}->{G.number_of_nodes()} nodes, "
          f"{e0}->{G.number_of_edges()} edges")
    return G


def _parse_maxspeed(ms):
    """OSM maxspeed (str/list/None) -> km/h float (fallback 50)."""
    if ms is None:
        return 50.0
    if isinstance(ms, (list, tuple)):
        vals = [v for v in ms if v is not None]
        return _parse_maxspeed(vals[0]) if vals else 50.0
    s = str(ms).strip().lower()
    m = re.search(r"(\d+(?:\.\d+)?)", s)
    if not m:
        return 50.0
    val = float(m.group(1))
    return val * 1.60934 if "mph" in s else val


def _set_time_minutes(G, speed_kmh):
    """Zet edge-attribuut `time` = reistijd in minuten."""
    m_per_min = speed_kmh * 1000.0 / 60.0
    for _u, _v, _k, data in G.edges(keys=True, data=True):
        data["time"] = data.get("length", 0.0) / m_per_min
    return G


def add_drive_time(G):
    """`speed_kph` (mogelijk str/list na simplify) -> edge `time` in minuten."""
    for _u, _v, _k, data in G.edges(keys=True, data=True):
        speed_kmh = _parse_maxspeed(data.get("speed_kph"))
        data["speed_kph"] = speed_kmh
        data["time"] = data.get("length", 0.0) / (speed_kmh * 1000.0 / 60.0)
    return G


# ---------------------------------------------------------------------------
# Isochroon-polygonen
# ---------------------------------------------------------------------------
def _buffer_union(G, edge_buff, node_filter=None):
    """Unie van gebufferde edges (= netwerk-buffer, geen convex hull).

    `node_filter` (optioneel): een set van node-id's; alleen edges waarvan beide
    uiteinden in de set zitten worden gebufferd. Voorkomt een dure
    ``G.subgraph(...)``-kopie bij grote (walk-)grafen.
    """
    if node_filter is None:
        edges = G.edges()
    else:
        edges = ((u, v) for u, v in G.edges()
                 if u in node_filter and v in node_filter)
    lines = [LineString([(G.nodes[u]["x"], G.nodes[u]["y"]),
                         (G.nodes[v]["x"], G.nodes[v]["y"])])
             for u, v in edges]
    if not lines:
        return None
    # quad_segs=2 houdt het aantal bufferhoeken laag (sneller + makkelijker te
    # vereenvoudigen) zonder de corridor-vorm wezenlijk te veranderen.
    #
    # Chunked buffering + unary_union: één groot MultiLineString.buffer() over
    # ~100k edges laat GEOS één reusachtig buffer+union in één keer draaien en
    # piekt daarbij richting de geheugenlimiet. Door in brokken van ~20k edges te
    # bufferen en die daarna incrementeel te verenigen blijft de piek beperkt tot
    # één brok, en uniont GEOS de delen stapsgewijs.
    CHUNK = 20000
    parts = [MultiLineString(lines[i:i + CHUNK]).buffer(edge_buff, quad_segs=2)
             for i in range(0, len(lines), CHUNK)]
    if len(parts) == 1:
        return parts[0]
    return unary_union(parts)


def isochrone_polys(G, dmap):
    """dmap: node -> reistijd (min). Return {break_min: poly|None} cumulatief."""
    out = {}
    for t in BREAKS:
        nodes = [n for n, d in dmap.items() if d <= t]
        poly = _buffer_union(G.subgraph(nodes), EDGE_BUFF) if nodes else None
        out[t] = _fill_holes(simplify(poly)) if poly is not None else None
    return out


def simplify(poly):
    s = poly.simplify(SIMPLIFY_TOL, preserve_topology=True)
    return poly if s.is_empty else s


# Gaten kleiner dan deze oppervlakte (m²) worden dichtgemaakt — de ~500 m²
# artefacten en vergelijkbare kleine holtes, maar grote legitieme gaten
# (meren, grote parken) en de buitenring blijven intact.
MAX_HOLE_AREA = 5000


def _fill_holes_polygon(poly, max_area):
    """Houd de buitenring; drop interior-rings ≤ max_area m²."""
    ext = poly.exterior
    holes = [h for h in poly.interiors if Polygon(h).area > max_area]
    return Polygon(ext, holes)


def _fill_holes(poly, max_area=MAX_HOLE_AREA):
    """Verwijdert kleine interior-rings (≤ max_area m²) zonder de buitenring te
    veranderen. MultiPolygon: per deel."""
    if poly is None or poly.is_empty:
        return poly
    if poly.geom_type == "Polygon":
        return _fill_holes_polygon(poly, max_area)
    if poly.geom_type == "MultiPolygon":
        return MultiPolygon([_fill_holes_polygon(p, max_area)
                             for p in poly.geoms])
    return poly


# ---------------------------------------------------------------------------
# Grid
# ---------------------------------------------------------------------------
def build_focus_points(boundary, spacing):
    minx, miny, maxx, maxy = boundary.bounds
    xs = np.arange(minx, maxx, spacing)
    ys = np.arange(miny, maxy, spacing)
    return [Point(x, y) for x in xs for y in ys if boundary.contains(Point(x, y))]


# ---------------------------------------------------------------------------
# Per gebied
# ---------------------------------------------------------------------------
def process_area(area):
    slug = area["slug"]
    boundary = areas.area_boundary(area)
    focus_pts = build_focus_points(boundary, area["spacing"])
    focus_ids = [f"f{i:04d}" for i in range(len(focus_pts))]
    print(f"\n[{slug}] {area['name']} — focuspunten ({area['spacing']}m grid): {len(focus_pts)}")
    if not focus_pts:
        print("  geen focuspunten, overslaan")
        return

    print("  netwerk laden…")
    drive_G, walk_G = load_networks(boundary, DRIVE_MARGIN, WALK_MARGIN)
    drive_G = add_drive_time(drive_G)
    walk_G = _set_time_minutes(walk_G, WALK_SPEED_KMH)

    xs = [p.x for p in focus_pts]
    ys = [p.y for p in focus_pts]
    drive_nodes = list(nearest_nodes(drive_G, xs, ys))
    walk_nodes = list(nearest_nodes(walk_G, xs, ys))

    features = []
    for i, (pid, dn, wn) in enumerate(zip(focus_ids, drive_nodes, walk_nodes)):
        d_drive = nx.single_source_dijkstra_path_length(drive_G, dn, weight="time")
        d_walk = nx.single_source_dijkstra_path_length(walk_G, wn, weight="time")

        # fiets = zelfde netwerk als lopen, maar 3× zo snel (tijd / 3).
        drive_polys = isochrone_polys(drive_G, d_drive)
        walk_polys = isochrone_polys(walk_G, d_walk)
        fiets_polys = isochrone_polys(walk_G, {n: d / BIKE_SPEED_RATIO
                                               for n, d in d_walk.items()})

        for mode, polys in [("auto", drive_polys), ("lopen", walk_polys),
                            ("fiets", fiets_polys)]:
            for t in BREAKS:
                if polys[t] is not None:
                    features.append({"focus_id": pid, "mode": mode,
                                     "minuten": t, "geometry": polys[t]})
        if (i + 1) % 10 == 0:
            print(f"  {i+1}/{len(focus_ids)} focuspunten verwerkt")

    # GeoJSON schrijven (vereenvoudigd)
    geojson = {"type": "FeatureCollection",
               "crs": {"type": "name", "properties": {"name": "EPSG:28992"}},
               "features": []}
    for feat in features:
        g = feat["geometry"]
        rings = _rings_of(g)
        coords = [[[round(x, 1), round(y, 1)] for x, y in ring] for ring in rings]
        geojson["features"].append({
            "type": "Feature",
            "properties": {"focus_id": feat["focus_id"], "mode": feat["mode"],
                           "minuten": feat["minuten"]},
            "geometry": {"type": "Polygon" if g.geom_type == "Polygon" else "MultiPolygon",
                         "coordinates": coords if g.geom_type == "Polygon" else [coords]},
        })

    out_iso = os.path.join(areas.OUT_DIR, f"{slug}_focus_isochrones.geojson")
    with open(out_iso, "w", encoding="utf-8") as f:
        json.dump(geojson, f)
    print(f"  geschreven: {os.path.basename(out_iso)} "
          f"({os.path.getsize(out_iso)/1e6:.2f} MB, {len(geojson['features'])} features)")

    points = {pid: {"x": round(p.x, 1), "y": round(p.y, 1)}
              for pid, p in zip(focus_ids, focus_pts)}
    out_pts = os.path.join(areas.OUT_DIR, f"{slug}_focus_points.json")
    with open(out_pts, "w", encoding="utf-8") as f:
        json.dump(points, f)
    print(f"  geschreven: {os.path.basename(out_pts)} ({len(points)} punten)")


def _rings_of(geom):
    if geom.is_empty:
        return []
    polys = [geom] if geom.geom_type == "Polygon" else list(geom.geoms)
    rings = []
    for p in polys:
        rings.append(list(p.exterior.coords))
        for hole in p.interiors:
            rings.append(list(hole.coords))
    return rings


def main():
    os.makedirs(areas.OUT_DIR, exist_ok=True)
    slugs = sys.argv[1:] or [a["slug"] for a in areas.NEW_AREAS]
    for slug in slugs:
        try:
            process_area(areas.get_area(slug))
        except Exception as e:  # noqa: BLE001 — doorgaan met de volgende gebieden
            print(f"\n!! [{slug}] mislukt: {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
