# -*- coding: utf-8 -*-
"""Reference-parity harness: replicate server.py's isochrone logic and dump
summaries (bbox / vertex count / area + KPI) for a fixed set of queries, so the
Rust server's output can be compared.

Run once (loads the PBF, ~3 min). Output: parity_python.json
"""

import os
import sys
import json

_RAILWAY = os.environ.get("RAILWAY_DEPLOY_DIR", r"C:\Users\Timo\Downloads\railway-deploy")
sys.path.insert(0, _RAILWAY)

import networkx as nx
from shapely.geometry import Point
from shapely import STRtree, make_valid
from shapely.ops import unary_union, transform as shapely_transform
from osmnx.distance import nearest_nodes

import isochrone_areas as areas
from build_focus_isochrones import (
    load_networks, add_drive_time, _set_time_minutes, _buffer_union, _fill_holes,
    simplify, EDGE_BUFF, WALK_SPEED_KMH, BIKE_SPEED_RATIO, _TO_RD, _TO_WGS84,
)

POP_FLOOR = 50
VIEWER_DIR = areas.OUT_DIR


def _load_supermarkten():
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
            out.append({"naam": s["naam"], "wvo": int(s["wvo"] or 0), "x": x, "y": y})
    return out


def _load_bag():
    seen = set()
    xs, ys, pops = [], [], []
    for a in areas.AREAS:
        p = os.path.join(VIEWER_DIR, f"{a['slug']}_bag_adressen.json")
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
    buurten, geoms = [], []
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


def _compute_kpi(S, poly):
    wvo = sum(S["supermarkten"][i]["wvo"]
              for i in S["sm_tree"].query(poly, predicate="contains"))
    pop_bag = sum(S["bag_pop"][i] for i in S["bag_tree"].query(poly, predicate="contains"))
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


def _rd_to_wgs84(geom):
    return shapely_transform(lambda x, y, z=None: _TO_WGS84.transform(x, y) + ((z,) if z is not None else ()), geom)


def _dijkstra(G, node):
    return nx.single_source_dijkstra_path_length(G, node, weight="time")


def poly_for(G, dmap, t):
    reachable = set(n for n, d in dmap.items() if d <= t)
    if not reachable:
        return None
    poly = _buffer_union(G, EDGE_BUFF, node_filter=reachable)
    if poly is None or poly.is_empty:
        return None
    return _fill_holes(simplify(poly))


def summarize(poly):
    if poly is None:
        return None
    wgs = _rd_to_wgs84(poly)
    minx, miny, maxx, maxy = wgs.bounds
    nverts = sum(len(r.coords) for p in (wgs.geoms if wgs.geom_type == "MultiPolygon" else [wgs]) for r in [p.exterior] + list(p.interiors))
    return {
        "bbox": [round(minx, 5), round(miny, 5), round(maxx, 5), round(maxy, 5)],
        "nverts": nverts,
        "area_rd": round(poly.area, 1),
    }


QUERIES = []
for (lon, lat) in [(5.198, 52.066), (5.121, 52.09), (5.164, 52.088),
                   (5.292, 52.132), (5.42, 51.975), (5.343, 51.97)]:
    for mode in ("auto", "fiets", "lopen"):
        for minuten in (10, 20):
            QUERIES.append((lon, lat, mode, minuten))


def main():
    t0 = __import__("time").time()
    region = unary_union([areas.area_boundary(a) for a in areas.AREAS])
    drive_G, walk_G = load_networks(region, areas.DRIVE_MARGIN, areas.WALK_MARGIN)
    drive_G = add_drive_time(drive_G)
    walk_G = _set_time_minutes(walk_G, WALK_SPEED_KMH)

    S = {}
    S["drive_G"], S["walk_G"] = drive_G, walk_G
    S["supermarkten"] = _load_supermarkten()
    S["sm_tree"] = STRtree([Point(s["x"], s["y"]) for s in S["supermarkten"]])
    bag_pop, bag_tree = _load_bag()
    S["bag_pop"], S["bag_tree"] = bag_pop, bag_tree
    buurten, geoms, buurt_tree = _load_buurten()
    S["buurten"] = buurten
    S["buurt_geoms"], S["buurt_tree"] = geoms, buurt_tree
    S["buurt_codes"] = [b["buurtcode"] for b in buurten]
    S["buurt_inw"] = [b["aantal_inwoners"] for b in buurten]
    S["buurt_areas"] = [g.area for g in geoms]
    print(f"[parity] loaded in {__import__('time').time()-t0:.0f}s", flush=True)

    results = []
    for (lon, lat, mode, minuten) in QUERIES:
        x, y = _TO_RD.transform(lon, lat)
        drive_node = nearest_nodes(drive_G, [x], [y])[0]
        walk_node = nearest_nodes(walk_G, [x], [y])[0]
        if mode == "auto":
            dmap = _dijkstra(drive_G, drive_node)
            G, t = drive_G, float(minuten)
        elif mode == "lopen":
            dmap = _dijkstra(walk_G, walk_node)
            G, t = walk_G, float(minuten)
        else:
            dmap = _dijkstra(walk_G, walk_node)
            G, t = walk_G, float(minuten) * BIKE_SPEED_RATIO
        poly = poly_for(G, dmap, t)
        kpi = _compute_kpi(S, poly) if poly is not None else {
            "wvo": 0.0, "pop_bag": 0.0, "pop_area": 0.0,
            "wvo_per_inwoner_bag": None, "wvo_per_inwoner_area": None,
            "buurt_dekking": {},
        }
        results.append({
            "query": {"lon": lon, "lat": lat, "mode": mode, "minuten": minuten},
            "geometry": summarize(poly),
            "kpi": kpi,
        })
        print(f"  {lon},{lat} {mode} {minuten}min done", flush=True)

    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "parity_python.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False)
    print(f"[parity] wrote {out} ({len(results)} results)", flush=True)


if __name__ == "__main__":
    main()
