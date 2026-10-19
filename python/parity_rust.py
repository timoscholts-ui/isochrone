# -*- coding: utf-8 -*-
"""Query the running Rust server for the same fixed query set as
parity_python.py and dump the same summary format (bbox / vertex count + KPI),
so the two JSON files can be diffed.

Usage: start the Rust server first, then:
    python parity_rust.py [--base http://127.0.0.1:8002]
"""

import json
import sys
import urllib.request
import urllib.parse

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8002"

QUERIES = []
for (lon, lat) in [(5.198, 52.066), (5.121, 52.09), (5.164, 52.088),
                   (5.292, 52.132), (5.42, 51.975), (5.343, 51.97)]:
    for mode in ("auto", "fiets", "lopen"):
        for minuten in (10, 20):
            QUERIES.append((lon, lat, mode, minuten))


def summarize(geom):
    if geom is None:
        return None
    coords = geom["coordinates"]

    def walk_poly(poly):
        return poly[0]  # exterior ring

    if geom["type"] == "Polygon":
        rings = coords
    else:
        rings = [p[0] for p in coords]  # exteriors only (bbox)
    pts = [c for ring in rings for c in ring]
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    nverts = sum(
        len(r) for poly in (coords if geom["type"] == "Polygon" else coords) for r in poly
    )
    return {
        "bbox": [round(min(xs), 5), round(min(ys), 5), round(max(xs), 5), round(max(ys), 5)],
        "nverts": nverts,
    }


def fetch(query):
    url = BASE + "/isochrone?" + urllib.parse.urlencode({
        "lat": query[1], "lon": query[0], "mode": query[2], "minuten": query[3],
    })
    with urllib.request.urlopen(url, timeout=600) as r:
        return json.load(r)


def main():
    results = []
    for q in QUERIES:
        try:
            d = fetch(q)
            kpi = d["kpi"]
            # Drop buurt_dekking for the summary comparison (order/keys can differ);
            # keep it separately if desired.
            results.append({
                "query": {"lon": q[0], "lat": q[1], "mode": q[2], "minuten": q[3]},
                "geometry": summarize(d.get("geometry")),
                "kpi": {k: v for k, v in kpi.items() if k != "buurt_dekking"},
            })
            print(f"  {q[0]},{q[1]} {q[2]} {q[3]}min done", flush=True)
        except Exception as e:  # noqa: BLE001
            results.append({
                "query": {"lon": q[0], "lat": q[1], "mode": q[2], "minuten": q[3]},
                "error": str(e),
            })
            print(f"  {q[0]},{q[1]} {q[2]} {q[3]}min ERROR: {e}", flush=True)

    out = "parity_rust.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False)
    print(f"wrote {out} ({len(results)} results)")


if __name__ == "__main__":
    main()
