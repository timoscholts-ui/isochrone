# -*- coding: utf-8 -*-
"""
Regio-heatmaps: WVO / inwoner per modaliteit × reistijd.

Leest de bestaande `_kpi_lookup.json` + `_focus_points_wgs84.json` van alle
gebieden (geen KPI-herberekening) en maakt per combinatie
(modaliteit × reistijd × metriek) een geïnterpoleerd raster:

  3 modaliteiten (auto / fiets / lopen)
  × 4 reistijden (5 / 10 / 15 / 20 min)
  × 2 metrieken (BAG / area)
  = 24 PNG's

Interpolatie gebeurt één keer in RD (EPSG:28992, metrisch en onvervormd) via
scipy.griddata (linear → nearest voor gaten). Cellen buiten de regio-unie worden
transparant.

Kleurschaal: divergerende brown → cream → teal ramp, met het neutrale punt
(cream) op WVO = 0.2 m²/inw (TwoSlopeNorm). Waarden onder 0.2 kleuren bruin,
erboven teal. Per metriek één gedeelde schaal zodat de vlakken vergelijkbaar
blijven.

Output in isochrone_viewer/:
  - heatmaps/hm_{mode}_{minuten}_{metric}.png  (24 stuks)
  - heatmaps/legend_{metric}.png                (2 kleurbalken)
  - heatmap_manifest.json                       (urls + bounds + norm + cmap)
  - heatmap_region.geojson                      (regio-unie in WGS84)

Geen netwerk; draait in enkele seconden.
"""

import os
import json

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.colors import LinearSegmentedColormap, TwoSlopeNorm
from pyproj import Transformer
from shapely.geometry import Point, mapping
from shapely.ops import unary_union, transform as shapely_transform
from shapely.prepared import prep
from scipy.interpolate import griddata

import isochrone_areas as areas

MODES = ["auto", "fiets", "lopen"]
MINUTEN = ["5", "10", "15", "20"]
METRICS = {"area": "wvo_per_inwoner_area"}
GRID_CELL = 250          # meter, rasterresolutie voor de interpolatie
TARGET_PX = 1400         # langste zijde van het uitvoer-PNG in pixels
V_CENTER = 0.2           # neutrale (drempel)waarde van de divergerende schaal

OUT_DIR = areas.OUT_DIR
HEAT_DIR = os.path.join(OUT_DIR, "heatmaps")

_TO_RD = Transformer.from_crs(4326, 28992, always_xy=True)
_TO_WGS84 = Transformer.from_crs(28992, 4326, always_xy=True)

# Divergerende kleurenramp (uit Sequential_divergingColorRamp.png):
# bruin (laag) → cream (neutraal) → teal (hoog).
_RAMP_STOPS = [
    (0.00, "#b24425"),
    (0.10, "#c56741"),
    (0.20, "#dc8e63"),
    (0.30, "#e6ac80"),
    (0.40, "#edcaa5"),
    (0.50, "#f0ebd7"),
    (0.60, "#c7d2b8"),
    (0.70, "#a2bea3"),
    (0.80, "#7ba996"),
    (0.90, "#438b81"),
    (1.00, "#136f6c"),
]
_CMAP = LinearSegmentedColormap.from_list("wvo_diverging", _RAMP_STOPS)
_CMAP.set_bad(alpha=0)   # gemaskeerde cellen → transparant


def load_data():
    """Laadt KPI + WGS84-focuspunten van alle gebieden."""
    kpi, pts = {}, {}
    for a in areas.AREAS:
        slug = a["slug"]
        with open(os.path.join(OUT_DIR, f"{slug}_kpi_lookup.json"),
                  encoding="utf-8") as f:
            kpi[slug] = json.load(f)
        with open(os.path.join(OUT_DIR, f"{slug}_focus_points_wgs84.json"),
                  encoding="utf-8") as f:
            pts[slug] = json.load(f)
    return kpi, pts


def pooled_vmin_vmax(kpi):
    """Gedeelde vmin/vmax per metriek, gepoold over alle niet-None waarden."""
    vals = {"bag": [], "area": []}
    for d in kpi.values():
        for modes in d.values():
            for mins in modes.values():
                for leaf in mins.values():
                    b = leaf.get("wvo_per_inwoner_bag")
                    a = leaf.get("wvo_per_inwoner_area")
                    if b is not None:
                        vals["bag"].append(b)
                    if a is not None:
                        vals["area"].append(a)
    return {m: (float(min(v)), float(max(v))) for m, v in vals.items()}


def build_grid(region):
    """Regelmatig RD-raster over region.bounds + een binnen-masker (eenmalig)."""
    minx, miny, maxx, maxy = region.bounds
    nx = int(round((maxx - minx) / GRID_CELL)) + 1
    ny = int(round((maxy - miny) / GRID_CELL)) + 1
    xs = np.linspace(minx, maxx, nx)
    ys = np.linspace(miny, maxy, ny)
    grid_x, grid_y = np.meshgrid(xs, ys)

    prepared = prep(region)
    inside = np.zeros(grid_x.shape, dtype=bool)
    for i in range(grid_x.shape[0]):
        for j in range(grid_x.shape[1]):
            inside[i, j] = prepared.contains(Point(grid_x[i, j], grid_y[i, j]))
    return grid_x, grid_y, inside, (minx, miny, maxx, maxy)


def interpolate(kpi, pts, grid_x, grid_y, mode, minuten, metric_key):
    """Verzamelt (x_rd, y_rd, waarde) en interpoleert naar het raster.

    Retourneert (z, n_punten) — z is een (ny, nx)-array zonder NaN.
    """
    xs, ys, vals = [], [], []
    for slug, d in kpi.items():
        focus_pts = pts[slug]
        for fid, modes in d.items():
            leaf = modes.get(mode, {}).get(minuten)
            if not leaf:
                continue
            v = leaf.get(metric_key)
            lonlat = focus_pts.get(fid)
            if v is None or lonlat is None:
                continue
            x, y = _TO_RD.transform(lonlat[0], lonlat[1])
            xs.append(x)
            ys.append(y)
            vals.append(v)

    if len(vals) == 0:
        return None, 0

    xs = np.asarray(xs)
    ys = np.asarray(ys)
    vals = np.asarray(vals, dtype=float)

    method = "linear" if len(vals) >= 4 else "nearest"
    z = griddata((xs, ys), vals, (grid_x, grid_y), method=method)
    nan_mask = np.isnan(z)
    if nan_mask.any():
        z_nn = griddata((xs, ys), vals, (grid_x, grid_y), method="nearest")
        z[nan_mask] = z_nn[nan_mask]
    return z, len(vals)


def render_surface(z_masked, out_png, bounds, norm):
    """Rendert een gemaskeerd raster naar PNG (RD-extent, gedeelde schaal)."""
    minx, miny, maxx, maxy = bounds
    ny, nx = z_masked.shape
    aspect = (maxx - minx) / (maxy - miny)   # breedte / hoogte
    if aspect >= 1:
        w_px = TARGET_PX
        h_px = max(1, int(round(TARGET_PX / aspect)))
    else:
        h_px = TARGET_PX
        w_px = max(1, int(round(TARGET_PX * aspect)))
    dpi = 100
    fig = plt.figure(frameon=False, figsize=(w_px / dpi, h_px / dpi), dpi=dpi)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_axis_off()
    ax.imshow(z_masked, extent=[minx, maxx, miny, maxy], origin="lower",
              cmap=_CMAP, norm=norm, aspect="auto", interpolation="bilinear")
    fig.savefig(out_png, transparent=True, dpi=dpi)
    plt.close(fig)


def render_legend(norm, out_png):
    """Horizontale kleurbalk (gradiënt) met dezelfde cmap+norm als de vlakken."""
    vals = np.linspace(norm.vmin, norm.vmax, 512).reshape(1, -1)
    fig = plt.figure(frameon=False, figsize=(5, 0.4))
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_axis_off()
    ax.imshow(vals, cmap=_CMAP, norm=norm, aspect="auto")
    fig.savefig(out_png, transparent=True, dpi=150)
    plt.close(fig)


def cmap_to_rgb(cmap, n=512):
    """Dichte sampling van de colormap voor kleur→waarde-inversie in de viewer."""
    return [[int(round(c * 255)) for c in cmap(i / (n - 1))[:3]]
            for i in range(n)]


def main():
    os.makedirs(HEAT_DIR, exist_ok=True)

    print("Laden KPI + focuspunten ...")
    kpi, pts = load_data()

    print("Regio-unie + raster ...")
    region = unary_union([areas.area_boundary(a) for a in areas.AREAS])
    grid_x, grid_y, inside, bounds = build_grid(region)
    minx, miny, maxx, maxy = bounds
    print(f"  bounds (RD): {minx:.0f}, {miny:.0f}, {maxx:.0f}, {maxy:.0f} "
          f"({grid_x.shape[1]}x{grid_x.shape[0]} cellen)")

    print("Gedeelde schaal per metriek (divergerend, centrum=0.2) ...")
    vminmax = pooled_vmin_vmax(kpi)
    norms = {}
    for m, (vmin, vmax) in vminmax.items():
        norms[m] = TwoSlopeNorm(vmin=vmin, vcenter=V_CENTER, vmax=vmax)
        print(f"  {m}: vmin={vmin:.4f}  vcenter={V_CENTER}  vmax={vmax:.4f}")

    # WGS84-bounds van de vier RD-hoeken van het raster.
    lons, lats = [], []
    for x, y in [(minx, miny), (maxx, miny), (minx, maxy), (maxx, maxy)]:
        lon, lat = _TO_WGS84.transform(x, y)
        lons.append(lon)
        lats.append(lat)
    wgs84_bounds = [[min(lats), min(lons)], [max(lats), max(lons)]]

    heatmaps = []
    for mode in MODES:
        for minuten in MINUTEN:
            for metric, metric_key in METRICS.items():
                z, n_pts = interpolate(kpi, pts, grid_x, grid_y,
                                       mode, minuten, metric_key)
                if z is None:
                    print(f"  !! {mode}/{minuten}/{metric}: geen punten — overgeslagen")
                    continue
                norm = norms[metric]
                z_masked = np.ma.masked_where(~inside, z)
                fname = f"hm_{mode}_{minuten}_{metric}.png"
                render_surface(z_masked, os.path.join(HEAT_DIR, fname),
                               bounds, norm)
                vals_inside = z[inside]
                print(f"  {mode:5s}/{minuten:2s}min/{metric:4s}: "
                      f"{n_pts:3d} punten, bereik "
                      f"{vals_inside.min():.3f}..{vals_inside.max():.3f}")
                heatmaps.append({"mode": mode, "minuten": minuten,
                                 "metric": metric, "url": f"heatmaps/{fname}",
                                 "bounds": wgs84_bounds})

    # Legenda-balken per metriek.
    for m, norm in norms.items():
        render_legend(norm, os.path.join(HEAT_DIR, f"legend_{m}.png"))

    manifest = {
        "cmap": cmap_to_rgb(_CMAP),
        "metrics": {
            "area": {"label": "WVO / inwoner (area-proportioneel, MAUP-gevoelig)",
                     "norm": {"vmin": vminmax["area"][0],
                              "vcenter": V_CENTER,
                              "vmax": vminmax["area"][1]},
                     "legend": "heatmaps/legend_area.png"},
        },
        "heatmaps": heatmaps,
    }
    with open(os.path.join(OUT_DIR, "heatmap_manifest.json"), "w",
              encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    region_wgs84 = shapely_transform(_TO_WGS84.transform, region)
    region_geojson = {
        "type": "FeatureCollection",
        "features": [{"type": "Feature", "properties": {"name": "regio"},
                      "geometry": mapping(region_wgs84)}],
    }
    with open(os.path.join(OUT_DIR, "heatmap_region.geojson"), "w",
              encoding="utf-8") as f:
        json.dump(region_geojson, f, ensure_ascii=False)

    print(f"\nKlaar: {len(heatmaps)} heatmaps + legenda's + manifest + regio-geojson "
          f"in {OUT_DIR}")


if __name__ == "__main__":
    main()
