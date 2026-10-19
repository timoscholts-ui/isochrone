# -*- coding: utf-8 -*-
"""
build_hexagons.py — flat-top hexagon tessellation + dasymetric CBS population for all areas.

Replicates the ArcGIS Pro workflow from Documentation_Hexagons_With_Statistics.docx
using free Python packages (geopandas + shapely, no ArcGIS, no AGOL):

  1. Generate a flat-top hexagon tessellation over each area's extent.
  2. CBS population / woonadres count = pop-per-address (dasymetric, per buurt).
  3. Spatial-join address points -> hexagons, SUM pop-per-address.

Areas come from `isochrone_areas` (gemeente OR wijk). One combined output layer.

Output (written to isochrone_areas.OUT_DIR):
  <output>.gpkg            — hexagons (EPSG:28992), fields: area, GRID_ID, Inwoners
  <output>.geojson         — same, EPSG:28992
  <output>_wgs84.geojson   — same, EPSG:4326 (ready for Leaflet)

Run (all areas):
  & "C:/Python314/python.exe" -X utf8 C:/Users/Timo/Downloads/build_hexagons.py
Run (subset):
  ... build_hexagons.py --areas houten zeist
"""

import argparse
import math
import os
import sqlite3

import geopandas as gpd
import pandas as pd
from shapely import STRtree
from shapely.geometry import Point, Polygon

import isochrone_areas as areas

SR_EPSG = 28992  # RD New
DEFAULT_AREA = 100000.0  # 10 ha, m^2


# ---------------------------------------------------------------------------
# Hexagon generation (flat-top)
# ---------------------------------------------------------------------------
def hex_side_from_area(area: float) -> float:
    return math.sqrt(2.0 * area / (3.0 * math.sqrt(3.0)))


def hex_vertices(cx: float, cy: float, s: float) -> list:
    pts = []
    for deg in (0, 60, 120, 180, 240, 300):
        rad = math.radians(deg)
        pts.append((cx + s * math.cos(rad), cy + s * math.sin(rad)))
    return pts


def excel_col(n: int) -> str:
    """Bijective base-26 column label: 0->A, 25->Z, 26->AA, ..."""
    out = ""
    n += 1
    while n > 0:
        n, rem = divmod(n - 1, 26)
        out = chr(65 + rem) + out
    return out


def generate_hexagons(study_geom, area: float) -> list:
    """Flat-top hexagon grid over the study-area bbox; keep cells intersecting it."""
    s = hex_side_from_area(area)
    minx, miny, maxx, maxy = study_geom.bounds

    dx = 1.5 * s
    dy = math.sqrt(3.0) * s
    q_min = int(math.floor((minx - s) / dx))
    q_max = int(math.ceil((maxx + s) / dx))

    hexagons = []
    for q in range(q_min, q_max + 1):
        cx = dx * q
        r_min = int(math.floor((miny - s) / dy - q / 2.0))
        r_max = int(math.ceil((maxy + s) / dy - q / 2.0))
        for r in range(r_min, r_max + 1):
            cy = dy * (r + q / 2.0)
            poly = Polygon(hex_vertices(cx, cy, s))
            if poly.intersects(study_geom):
                hexagons.append((poly, f"{excel_col(q)}-{r + 1}"))
    return hexagons


# ---------------------------------------------------------------------------
# Addresses with pop-per-address (dasymetric)
# ---------------------------------------------------------------------------
def compute_pop_per_address(boundary, buurten):
    """CBS buurt inwoners distributed evenly over BAG woonadressen per buurt.
    Returns (points, pops, total_inwoners, n_adressen)."""
    minx, miny, maxx, maxy = boundary.bounds
    con = sqlite3.connect(areas.BAG_SQLITE)
    cur = con.cursor()
    cur.execute(
        """
        SELECT x, y FROM vbos
        WHERE x BETWEEN ? AND ? AND y BETWEEN ? AND ?
          AND gebruiksdoelen LIKE '%woonfunctie%'
          AND status NOT LIKE '%ingetrokken%'
        """,
        (minx, maxx, miny, maxy),
    )
    rows = cur.fetchall()
    con.close()

    buurt_geoms = list(buurten.geometry)
    buurt_codes = list(buurten["buurtcode"])
    buurt_inw = [float(v or 0.0) for v in buurten["aantal_inwoners"]]
    tree = STRtree(buurt_geoms)

    adressen = []  # (x, y, buurtcode)
    for (x, y) in rows:
        p = Point(x, y)
        if not boundary.contains(p):
            continue
        bc = None
        for idx in sorted(tree.query(p)):
            if buurt_geoms[idx].contains(p):
                bc = buurt_codes[idx]
                break
        if bc is not None:
            adressen.append((x, y, bc))

    inw_by_buurt = dict(zip(buurt_codes, buurt_inw))
    n_by_buurt = {}
    for (_, _, bc) in adressen:
        n_by_buurt[bc] = n_by_buurt.get(bc, 0) + 1

    points, pops = [], []
    for (x, y, bc) in adressen:
        n = n_by_buurt.get(bc, 0)
        points.append(Point(x, y))
        pops.append((inw_by_buurt.get(bc, 0.0) / n) if n else 0.0)

    return points, pops, sum(buurt_inw), len(adressen)


def process_area(area, hex_area: float) -> gpd.GeoDataFrame:
    """Hexagons + Inwoners for one area. Returns GeoDataFrame (EPSG:28992)."""
    slug = area["slug"]
    boundary = areas.area_boundary(area)
    buurten = areas.area_buurten(area)

    points, pops, total_inwoners, n_adressen = compute_pop_per_address(boundary, buurten)
    hexagons = generate_hexagons(boundary, hex_area)

    hex_gdf = gpd.GeoDataFrame(
        {"area": slug, "GRID_ID": [gid for (_, gid) in hexagons]},
        geometry=[poly for (poly, _) in hexagons],
        crs=f"EPSG:{SR_EPSG}",
    )
    pt_gdf = gpd.GeoDataFrame({"pop": pops}, geometry=points, crs=f"EPSG:{SR_EPSG}")

    joined = gpd.sjoin(pt_gdf, hex_gdf, how="inner", predicate="within")
    summed = joined.groupby("GRID_ID")["pop"].sum()
    hex_gdf["Inwoners"] = hex_gdf["GRID_ID"].map(summed).fillna(0.0)

    print(f"[{slug}] {area['name']}: {len(hex_gdf):,} hexagons, {n_adressen:,} adressen, "
          f"SUM(Inwoners)={float(hex_gdf['Inwoners'].sum()):,.1f} (CBS={total_inwoners:,.1f})")
    return hex_gdf


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Hexagon tessellation + CBS population for all areas.")
    ap.add_argument("--area", type=float, default=DEFAULT_AREA, help="hexagon area in m^2 (default 100000 = 10 ha)")
    ap.add_argument("--areas", nargs="*", default=[a["slug"] for a in areas.AREAS],
                    help="area slugs to process (default: all)")
    ap.add_argument("--output", default="region_hexagons_inwoners", help="output basename")
    args = ap.parse_args()

    os.makedirs(areas.OUT_DIR, exist_ok=True)

    frames = []
    for slug in args.areas:
        frames.append(process_area(areas.get_area(slug), args.area))

    combined = gpd.GeoDataFrame(pd.concat(frames, ignore_index=True), crs=f"EPSG:{SR_EPSG}")

    gpkg = os.path.join(areas.OUT_DIR, args.output + ".gpkg")
    gj = os.path.join(areas.OUT_DIR, args.output + ".geojson")
    gj_wgs84 = os.path.join(areas.OUT_DIR, args.output + "_wgs84.geojson")

    combined.to_file(gpkg, driver="GPKG")
    combined.to_file(gj, driver="GeoJSON")
    combined.to_crs("EPSG:4326").to_file(gj_wgs84, driver="GeoJSON")

    print(f"\nTotal: {len(combined):,} hexagons across {combined['area'].nunique()} areas")
    print(f"Wrote {gpkg}")
    print(f"Wrote {gj}")
    print(f"Wrote {gj_wgs84}")


if __name__ == "__main__":
    main()
