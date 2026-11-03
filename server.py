# -*- coding: utf-8 -*-
"""
On-the-fly isochrone server (FastAPI).

Vervangt de precomputed isochronen + KPI door een Dijkstra-op-de-vlieg: laadt
éénmaal bij opstart het OSM drive/walk-netwerk voor de regio plus alle
supermarkten / BAG-adrespunten / buurten, en beantwoordt daarna
`GET /isochrone?lat=&lon=&mode=&minuten=` voor elke willekeurige klik.

Start: `python server.py` (serveert ook de statische viewer op
http://127.0.0.1:8000/ — vervangt `python -m http.server`).
"""

import os
import json
import time

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import networkx as nx
from shapely.geometry import Point, shape as shapely_shape
from shapely import STRtree, make_valid
from shapely.ops import unary_union, transform as shapely_transform
from osmnx.distance import nearest_nodes

import isochrone_areas as areas
from build_focus_isochrones import (
    load_networks,
    add_drive_time,
    _set_time_minutes,
    _buffer_union,
    _fill_holes,
    simplify,
    EDGE_BUFF,
    WALK_SPEED_KMH,
    BIKE_SPEED_RATIO,
    _TO_RD,
    _TO_WGS84,
)

DOWNLOADS_DIR = os.path.dirname(os.path.abspath(__file__))
VIEWER_DIR = areas.OUT_DIR  # isochrone_viewer/

# Minimale bevolking voor een betekenisvolle WVO-per-inwoner ratio (zie
# build_kpi_lookup.py).
POP_FLOOR = 50


# ---------------------------------------------------------------------------
# Startup data (éénmalig)
# ---------------------------------------------------------------------------
def _load_supermarkten():
    """Alle supermarkten (cross-regio, WGS84→RD), gedupliceerd op naam|adres."""
    seen = set()
    out = []
    for a in areas.AREAS:
        p = os.path.join(VIEWER_DIR, f"{a['slug']}_supermarkten.json")
        with open(p, encoding="utf-8") as f:
            lst = json.load(f)
        for s in lst:
            key = f"{s['naam']}|{s['adres']}"
            if key in seen:
                continue
            seen.add(key)
            x, y = _TO_RD.transform(s["lon"], s["lat"])
            out.append({"naam": s["naam"], "wvo": int(s["wvo"] or 0),
                        "x": x, "y": y})
    return out


def _load_bag():
    """BAG-adrespunten (RD), gedupliceerd op (x,y). Return (pops, STRtree).
    Returns empty data (pop_bag=0) gracefully if pre-computed files are absent."""
    seen = set()
    xs, ys, pops = [], [], []
    for a in areas.AREAS:
        p = os.path.join(VIEWER_DIR, f"{a['slug']}_bag_adressen.json")
        if not os.path.exists(p):
            print(f"[startup] BAG-bestand ontbreekt, overgeslagen: {p}", flush=True)
            continue
        with open(p, encoding="utf-8") as f:
            lst = json.load(f)
        for b in lst:
            key = (b["x"], b["y"])
            if key in seen:
                continue
            seen.add(key)
            xs.append(b["x"])
            ys.append(b["y"])
            pops.append(float(b["pop"]))
    points = [Point(x, y) for x, y in zip(xs, ys)]
    return pops, STRtree(points)


def _load_buurten():
    """De eigen buurten van alle 5 gebieden (RD). Return (buurten, geoms, tree)."""
    buurten = []  # dict: buurtcode, buurtnaam, aantal_inwoners, area
    geoms = []
    for a in areas.AREAS:
        bt = areas.area_buurten(a)
        for _, r in bt.iterrows():
            buurten.append({
                "buurtcode": r["buurtcode"],
                "buurtnaam": r["buurtnaam"] or "",
                "aantal_inwoners": max(0, int(r["aantal_inwoners"])),
                "area": a["slug"],
            })
            geoms.append(make_valid(r.geometry))
    return buurten, geoms, STRtree(geoms)


def _startup():
    t0 = time.time()

    print("[startup] netwerk laden (OSM pbf)…", flush=True)
    region = unary_union([areas.area_boundary(a) for a in areas.AREAS])
    drive_G, walk_G = load_networks(region, areas.DRIVE_MARGIN,
                                    areas.WALK_MARGIN)
    drive_G = add_drive_time(drive_G)
    walk_G = _set_time_minutes(walk_G, WALK_SPEED_KMH)
    print(f"[startup] netwerk klaar ({time.time()-t0:.0f}s)", flush=True)

    print("[startup] supermarkten laden…", flush=True)
    supermarkten = _load_supermarkten()
    sm_tree = STRtree([Point(s["x"], s["y"]) for s in supermarkten])
    print(f"[startup] supermarkten: {len(supermarkten)}", flush=True)

    print("[startup] BAG-adrespunten laden…", flush=True)
    bag_pop, bag_tree = _load_bag()
    print(f"[startup] BAG-punten: {len(bag_pop)}", flush=True)

    print("[startup] buurten laden…", flush=True)
    buurten, buurt_geoms, buurt_tree = _load_buurten()
    buurt_codes = [b["buurtcode"] for b in buurten]
    buurt_inw = [b["aantal_inwoners"] for b in buurten]
    buurt_areas = [g.area for g in buurt_geoms]
    print(f"[startup] buurten: {len(buurten)}", flush=True)

    print(f"[startup] klaar in {time.time()-t0:.0f}s", flush=True)

    return {
        "drive_G": drive_G, "walk_G": walk_G,
        "supermarkten": supermarkten, "sm_tree": sm_tree,
        "bag_pop": bag_pop, "bag_tree": bag_tree,
        "buurten": buurten, "buurt_geoms": buurt_geoms,
        "buurt_tree": buurt_tree, "buurt_codes": buurt_codes,
        "buurt_inw": buurt_inw, "buurt_areas": buurt_areas,
    }


S = _startup()

_dijkstra_cache = {}
_DIJKSTRA_CACHE_MAX = 64


def _dijkstra(G, name, node):
    key = (name, node)
    d = _dijkstra_cache.get(key)
    if d is None:
        d = nx.single_source_dijkstra_path_length(G, node, weight="time")
        if len(_dijkstra_cache) >= _DIJKSTRA_CACHE_MAX:
            _dijkstra_cache.clear()
        _dijkstra_cache[key] = d
    return d


def _poly_for_threshold(G, dmap, t):
    """Polygoon van alle nodes met reistijd ≤ t (gebufferde netwerk-unie)."""
    reachable = set(n for n, d in dmap.items() if d <= t)
    if not reachable:
        return None
    poly = _buffer_union(G, EDGE_BUFF, node_filter=reachable)
    if poly is None or poly.is_empty:
        return None
    return _fill_holes(simplify(poly))


def _rd_to_wgs84(geom):
    def _t(x, y, z=None):
        lon, lat = _TO_WGS84.transform(x, y)
        return (lon, lat) if z is None else (lon, lat, z)
    return shapely_transform(_t, geom)


def _geom_to_geojson(geom, ndigits=5):
    def ring(r):
        return [[round(x, ndigits), round(y, ndigits)] for x, y in r]
    if geom is None or geom.is_empty:
        return None
    if geom.geom_type == "Polygon":
        coords = [ring(geom.exterior.coords)]
        coords += [ring(h.coords) for h in geom.interiors]
        return {"type": "Polygon", "coordinates": coords}
    if geom.geom_type == "MultiPolygon":
        coords = []
        for p in geom.geoms:
            c = [ring(p.exterior.coords)]
            c += [ring(h.coords) for h in p.interiors]
            coords.append(c)
        return {"type": "MultiPolygon", "coordinates": coords}
    return None


def _compute_kpi(poly):
    wvo = 0.0
    for i in S["sm_tree"].query(poly, predicate="contains"):
        wvo += S["supermarkten"][i]["wvo"]

    pop_bag = 0.0
    for i in S["bag_tree"].query(poly, predicate="contains"):
        pop_bag += S["bag_pop"][i]

    dek = {}
    pop_area = 0.0
    for idx in S["buurt_tree"].query(poly, predicate="intersects"):
        inter = poly.intersection(S["buurt_geoms"][idx]).area
        if inter > 0:
            frac = inter / S["buurt_areas"][idx]
            dek[S["buurt_codes"][idx]] = round(frac, 4)
            pop_area += S["buurt_inw"][idx] * frac

    return {
        "wvo": round(wvo, 1),
        "pop_bag": round(pop_bag, 1),
        "pop_area": round(pop_area, 1),
        "wvo_per_inwoner_bag": round(wvo / pop_bag, 4) if pop_bag >= POP_FLOOR else None,
        "wvo_per_inwoner_area": round(wvo / pop_area, 4) if pop_area >= POP_FLOOR else None,
        "buurt_dekking": dek,
    }


# ---------------------------------------------------------------------------
# App + routes
# ---------------------------------------------------------------------------
app = FastAPI(title="Isochrone on-the-fly")

# Sta cross-origin requests toe: de frontend draait op Netlify, de API op deze
# server. Vervang "*" door je Netlify-domein als je de origin wilt beperken.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/isochrone")
def isochrone(lat: float, lon: float, mode: str = "auto", minuten: int = 10):
    if mode not in ("auto", "fiets", "lopen"):
        raise HTTPException(status_code=400, detail="mode moet auto/fiets/lopen zijn")
    minuten = max(1, min(20, int(minuten)))

    x, y = _TO_RD.transform(lon, lat)
    drive_node = nearest_nodes(S["drive_G"], [x], [y])[0]
    walk_node = nearest_nodes(S["walk_G"], [x], [y])[0]

    if mode == "auto":
        dmap = _dijkstra(S["drive_G"], "drive", drive_node)
        G, t = S["drive_G"], float(minuten)
    elif mode == "lopen":
        dmap = _dijkstra(S["walk_G"], "walk", walk_node)
        G, t = S["walk_G"], float(minuten)
    else:  # fiets = walk-netwerk op 3× snelheid
        dmap = _dijkstra(S["walk_G"], "walk", walk_node)
        G, t = S["walk_G"], float(minuten) * BIKE_SPEED_RATIO

    poly = _poly_for_threshold(G, dmap, t)
    kpi = _compute_kpi(poly) if poly is not None else {
        "wvo": 0.0, "pop_bag": 0.0, "pop_area": 0.0,
        "wvo_per_inwoner_bag": None, "wvo_per_inwoner_area": None,
        "buurt_dekking": {},
    }
    result = {
        "geometry": _geom_to_geojson(_rd_to_wgs84(poly)) if poly is not None else None,
        "kpi": kpi,
        "focus": {"x": round(x, 1), "y": round(y, 1)},
    }
    return result


class PolygonRequest(BaseModel):
    geometry: dict  # GeoJSON geometry (WGS84)


@app.post("/kpi_polygon")
def kpi_polygon(req: PolygonRequest):
    try:
        geom_wgs84 = shapely_shape(req.geometry)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid geometry: {e}")

    def _to_rd(x, y, z=None):
        rx, ry = _TO_RD.transform(x, y)
        return (rx, ry) if z is None else (rx, ry, z)

    poly_rd = make_valid(shapely_transform(_to_rd, geom_wgs84))
    return {"kpi": _compute_kpi(poly_rd)}


# Serveer de statische viewer (vervangt `python -m http.server`). De API-route
# hierboven is eerder geregistreerd en wint het dus van deze root-mount.
app.mount("/", StaticFiles(directory=DOWNLOADS_DIR, html=True), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")
