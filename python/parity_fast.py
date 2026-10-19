# -*- coding: utf-8 -*-
"""Fast Python reference for parity: skips the slow 20-min walk/fiets queries
(full-graph buffer). Computes KPI + geometry summary for the fast queries and
dumps parity_python_fast.json, comparable to parity_rust.json.
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
    seen, out = set(), []
    for a in areas.AREAS:
        with open(os.path.join(VIEWER_DIR, f"{a['slug']}_supermarkten.json"), encoding="utf-8") as f:
            for s in json.load(f):
                k = f"{s['naam']}|{s['adres']}"
                if k in seen:
                    continue
                seen.add(k)
                x, y = _TO_RD.transform(s["lon"], s["lat"])
                out.append({"wvo": int(s["wvo"] or 0), "x": x, "y": y})
    return out


def _load_bag():
    seen, xs, ys, pops = set(), [], [], []
    for a in areas.AREAS:
        with open(os.path.join(VIEWER_DIR, f"{a['slug']}_bag_adressen.json"), encoding="utf-8") as f:
            for b in json.load(f):
                k = (b["x"], b["y"])
                if k in seen:
                    continue
                seen.add(k)
                xs.append(b["x"]); ys.append(b["y"]); pops.append(float(b["pop"]))
    return pops, STRtree([Point(x, y) for x, y in zip(xs, ys)])


def _load_buurten():
    buurten, geoms = [], []
    for a in areas.AREAS:
        for _, r in areas.area_buurten(a).iterrows():
            buurten.append({"buurtcode": r["buurtcode"], "aantal_inwoners": max(0, int(r["aantal_inwoners"]))})
            geoms.append(make_valid(r.geometry))
    return buurten, geoms, STRtree(geoms)


def _compute_kpi(S, poly):
    wvo = sum(S["sm"][i]["wvo"] for i in S["sm_tree"].query(poly, predicate="contains"))
    pop_bag = sum(S["bag_pop"][i] for i in S["bag_tree"].query(poly, predicate="contains"))
    pop_area = 0.0
    for idx in S["bt_tree"].query(poly, predicate="intersects"):
        inter = poly.intersection(S["bt_geoms"][idx]).area
        if inter > 0:
            pop_area += S["bt_inw"][idx] * (inter / S["bt_areas"][idx])
    return {
        "wvo": round(wvo, 1), "pop_bag": round(pop_bag, 1), "pop_area": round(pop_area, 1),
        "wvo_per_inwoner_bag": round(wvo / pop_bag, 4) if pop_bag >= POP_FLOOR else None,
        "wvo_per_inwoner_area": round(wvo / pop_area, 4) if pop_area >= POP_FLOOR else None,
    }


def summarize(poly):
    if poly is None:
        return None
    wgs = shapely_transform(lambda x, y, z=None: _TO_WGS84.transform(x, y) + ((z,) if z is not None else ()), poly)
    minx, miny, maxx, maxy = wgs.bounds
    polys = wgs.geoms if wgs.geom_type == "MultiPolygon" else [wgs]
    nverts = sum(len(r.coords) for p in polys for r in [p.exterior] + list(p.interiors))
    return {"bbox": [round(minx, 5), round(miny, 5), round(maxx, 5), round(maxy, 5)], "nverts": nverts}


QUERIES = []
for (lon, lat) in [(5.198, 52.066), (5.121, 52.09), (5.164, 52.088),
                   (5.292, 52.132), (5.42, 51.975), (5.343, 51.97)]:
    for mode in ("auto", "fiets", "lopen"):
        for minuten in (10, 20):
            if mode != "auto" and minuten == 20:
                continue  # skip slow walk/fiets 20-min
            QUERIES.append((lon, lat, mode, minuten))


def main():
    import time
    t0 = time.time()
    region = unary_union([areas.area_boundary(a) for a in areas.AREAS])
    drive_G, walk_G = load_networks(region, areas.DRIVE_MARGIN, areas.WALK_MARGIN)
    drive_G = add_drive_time(drive_G)
    walk_G = _set_time_minutes(walk_G, WALK_SPEED_KMH)
    S = {"drive_G": drive_G, "walk_G": walk_G}
    S["sm"] = _load_supermarkten()
    S["sm_tree"] = STRtree([Point(s["x"], s["y"]) for s in S["sm"]])
    S["bag_pop"], S["bag_tree"] = _load_bag()
    bt, geoms, bt_tree = _load_buurten()
    S["bt_geoms"], S["bt_tree"] = geoms, bt_tree
    S["bt_inw"] = [b["aantal_inwoners"] for b in bt]
    S["bt_areas"] = [g.area for g in geoms]
    print(f"[fast] loaded in {time.time()-t0:.0f}s", flush=True)

    results = []
    for (lon, lat, mode, minuten) in QUERIES:
        x, y = _TO_RD.transform(lon, lat)
        dn = nearest_nodes(drive_G, [x], [y])[0]
        wn = nearest_nodes(walk_G, [x], [y])[0]
        if mode == "auto":
            dmap = nx.single_source_dijkstra_path_length(drive_G, dn, weight="time")
            G, t = drive_G, float(minuten)
        elif mode == "lopen":
            dmap = nx.single_source_dijkstra_path_length(walk_G, wn, weight="time")
            G, t = walk_G, float(minuten)
        else:
            dmap = nx.single_source_dijkstra_path_length(walk_G, wn, weight="time")
            G, t = walk_G, float(minuten) * BIKE_SPEED_RATIO
        reachable = set(n for n, d in dmap.items() if d <= t)
        poly = None
        if reachable:
            poly = _buffer_union(G, EDGE_BUFF, node_filter=reachable)
            if poly is not None and not poly.is_empty:
                poly = _fill_holes(simplify(poly))
            else:
                poly = None
        kpi = _compute_kpi(S, poly) if poly is not None else {"wvo": 0.0, "pop_bag": 0.0, "pop_area": 0.0,
                                                              "wvo_per_inwoner_bag": None, "wvo_per_inwoner_area": None}
        results.append({"query": {"lon": lon, "lat": lat, "mode": mode, "minuten": minuten},
                        "geometry": summarize(poly), "kpi": kpi})
        print(f"  {lon},{lat} {mode} {minuten}min done", flush=True)

    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "parity_python_fast.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False)
    print(f"wrote {out} ({len(results)} results)", flush=True)


if __name__ == "__main__":
    main()
