# -*- coding: utf-8 -*-
"""Regenerate buurten data from the shared CBS 2026 loader (isochrone_areas).

Reuses `railway-deploy/isochrone_areas.py` (WijkBuurtkaart 2026 GPKG + StatLine
population) so the server and frontend buurten match `export_graphs.py`.

Outputs:
  - <OUT_DIR>/buurten.json                     (server, RD EPSG:28992)
  - <VIEWER_OUT>/{slug}_buurten_wgs84.geojson  (frontend, WGS84 EPSG:4326)
"""

import os
import sys
import json

from shapely import make_valid
from shapely.geometry import mapping

RAILWAY_DEPLOY_DIR = os.environ.get(
    "RAILWAY_DEPLOY_DIR", r"C:\Users\Timo\Downloads\railway-deploy"
)
sys.path.insert(0, RAILWAY_DEPLOY_DIR)

import isochrone_areas as areas

OUT_DIR = os.environ.get("OUT_DIR", r"C:\Users\Timo\Downloads\isochrone-rust\data")
VIEWER_OUT = os.environ.get(
    "VIEWER_OUT", r"C:\Users\Timo\Downloads\netlify_deploy\isochrone_viewer"
)


def main():
    # server buurten.json (RD)
    buurten = []
    for a in areas.AREAS:
        sub = areas.area_buurten(a)
        for _, r in sub.iterrows():
            buurten.append({
                "buurtcode": r["buurtcode"],
                "buurtnaam": r["buurtnaam"] or "",
                "aantal_inwoners": int(r["aantal_inwoners"]),
                "area": a["slug"],
                "geometry": mapping(make_valid(r.geometry)),
            })
    out_json = os.path.join(OUT_DIR, "buurten.json")
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(buurten, f, ensure_ascii=False)
    print(f"[buurten] wrote {out_json} ({len(buurten)} buurten)")

    # frontend buurten_wgs84.geojson (WGS84), per slug
    os.makedirs(VIEWER_OUT, exist_ok=True)
    for a in areas.AREAS:
        sub = areas.area_buurten(a).to_crs(4326)
        features = []
        for _, r in sub.iterrows():
            features.append({
                "type": "Feature",
                "properties": {
                    "buurtcode": r["buurtcode"],
                    "buurtnaam": r["buurtnaam"] or "",
                    "aantal_inwoners": int(r["aantal_inwoners"]),
                },
                "geometry": mapping(make_valid(r.geometry)),
            })
        fc = {"type": "FeatureCollection", "features": features}
        out = os.path.join(VIEWER_OUT, f"{a['slug']}_buurten_wgs84.geojson")
        with open(out, "w", encoding="utf-8") as f:
            json.dump(fc, f, ensure_ascii=False)
        print(f"[buurten] wrote {out} ({len(features)} features)")


if __name__ == "__main__":
    main()
