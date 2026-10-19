# -*- coding: utf-8 -*-
"""
Stap 4 — reprojecteer de weergave-data naar WGS84 voor Leaflet (alle gebieden).

Leaflet werkt in WGS84 (lat/lon); onze data staat in RD New (EPSG:28992).
Dit script maakt per nieuw gebied WGS84-kopieën van isochronen, buurten en
focuspunten, en extraheert de supermarktlocaties (lat/lon uit de GPKG).

Output per nieuw gebied in isochrone_viewer/:
  - <slug>_isochrones_wgs84.geojson
  - <slug>_buurten_wgs84.geojson
  - <slug>_focus_points_wgs84.json
  - <slug>_supermarkten.json

Daarnaast: areas.json — manifest voor de viewer (alle gebieden, incl. Bunnik).
"""

import os
import sys
import json

import geopandas as gpd
from shapely.ops import transform
from pyproj import Transformer

import isochrone_areas as areas

_TO_WGS84 = Transformer.from_crs(28992, 4326, always_xy=True)


def round_coords(geom, ndigits=6):
    def _round(x, y, z=None):
        if z is None:
            return (round(x, ndigits), round(y, ndigits))
        return (round(x, ndigits), round(y, ndigits), round(z, ndigits))
    return transform(_round, geom)


def process_area(area):
    slug = area["slug"]
    print(f"\n[{slug}] {area['name']}")

    # --- isochronen ---
    iso_rd = os.path.join(areas.OUT_DIR, f"{slug}_focus_isochrones.geojson")
    iso = gpd.read_file(iso_rd)
    iso = iso.to_crs(4326)
    iso["geometry"] = iso["geometry"].apply(lambda g: round_coords(g, 5))
    out_iso = os.path.join(areas.OUT_DIR, f"{slug}_isochrones_wgs84.geojson")
    iso.to_file(out_iso, driver="GeoJSON")
    print(f"  isochronen WGS84: {os.path.basename(out_iso)} "
          f"({os.path.getsize(out_iso)/1e6:.2f} MB, {len(iso)} features)")

    # --- buurten ---
    buurten = areas.area_buurten(area)
    buurten = buurten[["buurtcode", "buurtnaam", "aantal_inwoners", "geometry"]].to_crs(4326)
    buurten["geometry"] = buurten["geometry"].apply(lambda g: round_coords(g, 5))
    out_b = os.path.join(areas.OUT_DIR, f"{slug}_buurten_wgs84.geojson")
    buurten.to_file(out_b, driver="GeoJSON")
    print(f"  buurten WGS84: {os.path.basename(out_b)} ({len(buurten)} buurten)")

    # --- focuspunten ---
    pts_rd_path = os.path.join(areas.OUT_DIR, f"{slug}_focus_points.json")
    with open(pts_rd_path, encoding="utf-8") as f:
        pts_rd = json.load(f)
    pts = {pid: list(_TO_WGS84.transform(v["x"], v["y"]))
           for pid, v in pts_rd.items()}
    out_p = os.path.join(areas.OUT_DIR, f"{slug}_focus_points_wgs84.json")
    with open(out_p, "w", encoding="utf-8") as f:
        json.dump(pts, f)
    print(f"  focuspunten WGS84: {os.path.basename(out_p)} ({len(pts)} punten)")

    # --- supermarkten (cross-regio, lat/lon uit GPKG) ---
    sm = areas.area_supermarkten_broad(area)
    supermarkten = [
        {"naam": r["NAAM"], "wvo": int(r["WVO"] or 0),
         "lat": float(r["lat"]), "lon": float(r["lon"]),
         "adres": f"{r['STRAAT']} {r['HUISNR']}{(r['HUISNRTOEV'] or '').strip()}, "
                  f"{r['POSTCODE']} {r['WOONPLAATS']}"}
        for _, r in sm.iterrows()
    ]
    out_s = os.path.join(areas.OUT_DIR, f"{slug}_supermarkten.json")
    with open(out_s, "w", encoding="utf-8") as f:
        json.dump(supermarkten, f, ensure_ascii=False)
    print(f"  supermarkten: {os.path.basename(out_s)} ({len(supermarkten)})")


def write_manifest():
    manifest = [
        {"slug": a["slug"], "name": a["name"]}
        for a in areas.AREAS
    ]
    out = os.path.join(areas.OUT_DIR, "areas.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    print(f"\nmanifest: {os.path.basename(out)} ({len(manifest)} gebieden)")


def main():
    os.makedirs(areas.OUT_DIR, exist_ok=True)
    slugs = sys.argv[1:] or [a["slug"] for a in areas.NEW_AREAS]
    for slug in slugs:
        try:
            process_area(areas.get_area(slug))
        except Exception as e:  # noqa: BLE001 — doorgaan met de volgende gebieden
            print(f"\n!! [{slug}] mislukt: {type(e).__name__}: {e}")
    write_manifest()


if __name__ == "__main__":
    main()
