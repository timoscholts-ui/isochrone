# -*- coding: utf-8 -*-
"""Phase A precompute: dump the OSM drive/walk graphs + buurten to files the
Rust server loads fast.

Reuses the existing (known-good) Python network loader so Phase A delivers the
performance win without porting the PBF logic yet. Run once (offline / in a
build job); the Rust server then never touches pyrosm/osmnx/networkx.

Outputs (to OUT_DIR env, default ``../data``):
  - drive.graph.bin   -- binary graph (see ISOG format below)
  - walk.graph.bin
  - buurten.json      -- per-area buurten (code/naam/inwoners + GeoJSON RD geom)

ISOG binary format (little-endian, all widths fixed):
  offset 0   : b"ISOG" magic (4 bytes)
  offset 4   : u32 version = 1
  offset 8   : u64 node_count
  offset 16  : u64 edge_count
  offset 24  : node_count * (u64 id, f64 x, f64 y)            = 24 bytes each
  then       : edge_count * (u64 from, u64 to, f64 length, f64 time, f64 speed_kph)
               = 40 bytes each
"""

import os
import sys
import json
import struct

_HERE = os.path.dirname(os.path.abspath(__file__))
_RAILWAY = os.environ.get(
    "RAILWAY_DEPLOY_DIR", r"C:\Users\Timo\Downloads\railway-deploy"
)
sys.path.insert(0, _RAILWAY)

import isochrone_areas as areas
from build_focus_isochrones import (
    load_networks,
    add_drive_time,
    _set_time_minutes,
    WALK_SPEED_KMH,
)
from shapely.ops import unary_union
from shapely import make_valid
from shapely.geometry import mapping

OUT_DIR = os.environ.get("OUT_DIR", os.path.join(_HERE, "..", "data"))


def _write_graph(path, G):
    nodes = [(int(n), float(d["x"]), float(d["y"]))
             for n, d in G.nodes(data=True)]
    edges = [(int(u), int(v),
              float(d.get("length", 0.0)),
              float(d.get("time", 0.0)),
              float(d.get("speed_kph", 0.0)))
             for u, v, _k, d in G.edges(keys=True, data=True)]
    with open(path, "wb") as f:
        f.write(b"ISOG")
        f.write(struct.pack("<I", 1))
        f.write(struct.pack("<Q", len(nodes)))
        f.write(struct.pack("<Q", len(edges)))
        for nid, x, y in nodes:
            f.write(struct.pack("<Qdd", nid, x, y))
        for u, v, length, t, s in edges:
            f.write(struct.pack("<QQddd", u, v, length, t, s))
    return len(nodes), len(edges)


def _write_buurten(path):
    buurten = []
    seen = set()
    for a in areas.AREAS:
        bt = areas.area_buurten(a)
        for _, r in bt.iterrows():
            code = r["buurtcode"]
            if code in seen:
                continue
            seen.add(code)
            buurten.append({
                "buurtcode": code,
                "buurtnaam": r["buurtnaam"] or "",
                "aantal_inwoners": max(0, int(r["aantal_inwoners"])),
                "area": a["slug"],
                "geometry": mapping(make_valid(r.geometry)),
            })
    with open(path, "w", encoding="utf-8") as f:
        json.dump(buurten, f, ensure_ascii=False)
    return len(buurten)


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    print(f"[export] regio: unie van {len(areas.AREAS)} gebieden", flush=True)

    region = unary_union([areas.area_boundary(a) for a in areas.AREAS])
    print(f"[export] netwerk laden (OSM pbf, drive-marge {areas.DRIVE_MARGIN} m, "
          f"walk-marge {areas.WALK_MARGIN} m)…", flush=True)
    drive_G, walk_G = load_networks(region, areas.DRIVE_MARGIN, areas.WALK_MARGIN)
    drive_G = add_drive_time(drive_G)
    walk_G = _set_time_minutes(walk_G, WALK_SPEED_KMH)

    p = os.path.join(OUT_DIR, "drive.graph.bin")
    nn, ne = _write_graph(p, drive_G)
    print(f"[export] drive.graph.bin: {nn} nodes, {ne} edges -> {p}", flush=True)

    p = os.path.join(OUT_DIR, "walk.graph.bin")
    nn, ne = _write_graph(p, walk_G)
    print(f"[export] walk.graph.bin: {nn} nodes, {ne} edges -> {p}", flush=True)

    p = os.path.join(OUT_DIR, "buurten.json")
    nb = _write_buurten(p)
    print(f"[export] buurten.json: {nb} buurten -> {p}", flush=True)
    print("[export] klaar", flush=True)


if __name__ == "__main__":
    main()
