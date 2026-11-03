# -*- coding: utf-8 -*-
"""
Drop-in replacement for isochrone_areas.py for NL-wide processing.

Data sources (changed from local GPKG to hosted services):
  - CBS buurten:  ArcGIS FeatureServer CBS_Buurten_2026 (layer 0, WGS84)
                  Fields: bu_code, bu_naam, gm_code, gm_naam, AantalInwoners
  - Supermarkten: C:\\Users\\Timo\\Downloads\\Supermarkt_lidl (2).gpkg  (EPSG:28992)

Usage (from nl_worker.py):
    import nl_areas
    nl_areas.set_current_area("GM0363", 10000, 5000, "Utrecht", 1000)
    sys.modules["isochrone_areas"] = nl_areas
    importlib.reload(build_focus_isochrones)   # picks up monkey-patch
    importlib.reload(build_bag_population)
    ...

The module exposes the same API as isochrone_areas.py:
  - module-level attributes: AREAS, NEW_AREAS, DRIVE_MARGIN, WALK_MARGIN,
    SUPERMARKT_MARGIN, OUT_DIR, CBS_GPKG, LOCATUS_GPKG, BAG_SQLITE
    (all returned dynamically via PEP 562 __getattr__ from _state)
  - functions: get_area, area_boundary, area_buurten, area_buurten_broad,
    area_supermarkten_broad

All 14,913 CBS buurten are fetched once per process and cached at module level.
Per-area boundary/buurten are cached in thread-local _state.
"""

import os
import threading
import urllib.parse
import urllib.request
import json

import geopandas as gpd
import pandas as pd
from shapely.ops import unary_union
from pyproj import Transformer

# ---------------------------------------------------------------------------
# Data source configuration
# ---------------------------------------------------------------------------
_CBS_FS_URL = os.environ.get(
    "CBS_BUURTEN_FS_URL",
    "https://services9.arcgis.com/e2SbRUv3uVfnVzwG/arcgis/rest/services/CBS_Buurten_2026/FeatureServer/0",
)
_AGOL_USERNAME = os.environ.get("AGOL_USERNAME", "timo_PerfectPlace")
_AGOL_PASSWORD = os.environ.get("AGOL_PASSWORD", "Espresso25!!")

_LOCATUS_GPKG_DEFAULT = os.environ.get(
    "LOCATUS_GPKG",
    r"C:\Users\Timo\Downloads\Supermarkt_lidl (2).gpkg",
)
_BAG_SQLITE_DEFAULT = os.environ.get(
    "BAG_SQLITE",
    r"C:\Users\Timo\Downloads\pcodes-sqlite\bag-export.sqlite",
)
_OUT_DIR_DEFAULT = os.environ.get(
    "OUT_DIR",
    r"C:\Users\Timo\Downloads\isochrone_viewer",
)

# Kept for __getattr__ compat — value is the FS URL, not a file path
_CBS_GPKG_DEFAULT = _CBS_FS_URL

# ---------------------------------------------------------------------------
# Thread-local state — holds the current area for this worker thread
# ---------------------------------------------------------------------------
_state = threading.local()

# ---------------------------------------------------------------------------
# Module-level CBS buurten cache (fetched once, all 14 913 buurten)
# ---------------------------------------------------------------------------
_cbs_lock = threading.Lock()
_cbs_buurten_gdf = None          # GeoDataFrame EPSG:28992, cached after first load

_TO_WGS84 = Transformer.from_crs(28992, 4326, always_xy=True)


# ---------------------------------------------------------------------------
# AGOL token (cached, re-requested on expiry)
# ---------------------------------------------------------------------------
_token_cache: dict = {"token": None, "expires": 0}
_token_lock = threading.Lock()


def _get_token() -> str:
    import time
    with _token_lock:
        if _token_cache["token"] and time.time() < _token_cache["expires"] - 60:
            return _token_cache["token"]
        data = urllib.parse.urlencode({
            "username": _AGOL_USERNAME,
            "password": _AGOL_PASSWORD,
            "referer": "https://www.arcgis.com",
            "expiration": 120,      # 2 hours in minutes
            "f": "json",
        }).encode()
        resp = json.loads(
            urllib.request.urlopen(
                urllib.request.Request(
                    "https://www.arcgis.com/sharing/rest/generateToken", data=data
                )
            ).read()
        )
        if "token" not in resp:
            raise RuntimeError(f"AGOL token error: {resp}")
        _token_cache["token"] = resp["token"]
        _token_cache["expires"] = time.time() + resp.get("expires", 7200) / 1000
        return _token_cache["token"]


def _query_fs_page(offset: int, token: str, out_fields: str = "*",
                   where: str = "1=1", return_geometry: bool = True) -> dict:
    """Fetch one page from the CBS buurten FeatureServer."""
    params = urllib.parse.urlencode({
        "where": where,
        "outFields": out_fields,
        "returnGeometry": "true" if return_geometry else "false",
        "resultOffset": offset,
        "resultRecordCount": 2000,
        "outSR": 4326,
        "f": "geojson",
        "token": token,
    })
    url = f"{_CBS_FS_URL}/query?{params}"
    return json.loads(urllib.request.urlopen(url, timeout=120).read())


def _fetch_all_buurten() -> gpd.GeoDataFrame:
    """
    Fetch all CBS buurten from the FeatureServer (paginated, WGS84 → RD28992).
    Result columns: buurtcode, buurtnaam, gemeentecode, gemeentenaam, aantal_inwoners, geometry
    """
    token = _get_token()
    features = []
    offset = 0
    while True:
        page = _query_fs_page(offset, token,
                              out_fields="bu_code,bu_naam,gm_code,gm_naam,AantalInwoners")
        batch = page.get("features", [])
        features.extend(batch)
        print(f"  CBS buurten: fetched {len(features)} ...")
        if len(batch) < 2000:
            break
        offset += 2000

    gdf = gpd.GeoDataFrame.from_features(features, crs=4326).to_crs(28992)
    gdf = gdf.rename(columns={
        "bu_code":       "buurtcode",
        "bu_naam":       "buurtnaam",
        "gm_code":       "gemeentecode",
        "gm_naam":       "gemeentenaam",
        "AantalInwoners": "aantal_inwoners",
    })
    gdf["aantal_inwoners"] = pd.to_numeric(gdf["aantal_inwoners"], errors="coerce").fillna(0)
    return gdf[["buurtcode", "buurtnaam", "gemeentecode", "gemeentenaam",
                "aantal_inwoners", "geometry"]]


def _cbs_buurten() -> gpd.GeoDataFrame:
    """Return the module-level cached CBS buurten GeoDataFrame (lazy load)."""
    global _cbs_buurten_gdf
    if _cbs_buurten_gdf is None:
        with _cbs_lock:
            if _cbs_buurten_gdf is None:
                print("Loading CBS buurten from FeatureServer (once per process)...")
                _cbs_buurten_gdf = _fetch_all_buurten()
                print(f"  Loaded {len(_cbs_buurten_gdf)} buurten.")
    return _cbs_buurten_gdf


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def set_current_area(
    buurt_code: str,
    drive_margin: int,
    walk_margin: int,
    buurt_naam: str,
    gemeente_code: str,
    gemeente_naam: str,
    spacing: int = 750,
) -> dict:
    """
    Set the current buurt as the job unit for this thread and return the area dict.

    The isochrone grid is built within the buurt boundary; OSM network loading
    covers only the buurt + margin, keeping RAM low per job.

    Must be called before any build_*.process_area() call.
    Clears per-area boundary and buurten caches.
    """
    slug = buurt_code.lower()  # bu00140000

    area = {
        "slug": slug,
        "name": buurt_naam,
        "gemeente": gemeente_naam,
        "gemeente_code": gemeente_code,
        "buurt_code": buurt_code,
        "spacing": spacing,
    }

    _state.area = area
    _state.drive_margin = drive_margin
    _state.walk_margin = walk_margin
    _state.supermarkt_margin = drive_margin
    _state.out_dir = os.environ.get("OUT_DIR", _OUT_DIR_DEFAULT)
    _state.boundary_cache = {}
    _state.buurten_cache = {}
    _state.areas_list = [area]

    return area


def set_all_areas(gemeente_list: list):
    """For build_heatmaps / export phase: set AREAS to the full list."""
    _state.areas_list = gemeente_list


def get_area(slug: str) -> dict:
    """Compatibility shim — returns the current area (slug ignored)."""
    return _state.area


# ---------------------------------------------------------------------------
# Geographic functions
# ---------------------------------------------------------------------------

def area_boundary(area):
    """
    Shapely geometry (EPSG:28992) of the buurt boundary.
    Looked up directly from the cached CBS buurten GeoDataFrame by buurt_code.
    Cached in thread-local _state per buurt_code.
    """
    code = area["buurt_code"]
    cache = getattr(_state, "boundary_cache", {})
    if code not in cache:
        bt = _cbs_buurten()
        rows = bt[bt["buurtcode"] == code]
        if rows.empty:
            raise KeyError(f"Buurt {code} niet gevonden in CBS FeatureServer")
        cache[code] = unary_union(rows.geometry.tolist())
        _state.boundary_cache = cache
    return cache[code]


def area_buurten(area):
    """
    GeoDataFrame (EPSG:28992) with just this single buurt.
    Used by build_viewer_data.py to write the buurt boundary to the viewer.
    Columns: buurtcode, buurtnaam, aantal_inwoners, geometry.
    """
    code = area["buurt_code"]
    cache = getattr(_state, "buurten_cache", {})
    if code not in cache:
        bt = _cbs_buurten()
        gdf = bt[bt["buurtcode"] == code].copy().reset_index(drop=True)
        gdf["aantal_inwoners"] = gdf["aantal_inwoners"].clip(lower=0)
        cache[code] = gdf[["buurtcode", "buurtnaam", "aantal_inwoners", "geometry"]]
        _state.buurten_cache = cache
    return cache[code]


def area_buurten_broad(area, margin=None):
    """
    GeoDataFrame with all buurten whose centroid falls within `margin` metres
    of the gemeente boundary (cross-municipality, for BAG coupling).
    Clips negative aantal_inwoners to 0.
    """
    if margin is None:
        margin = getattr(_state, "supermarkt_margin", 10000)
    bt = _cbs_buurten()
    boundary = area_boundary(area).buffer(margin)
    keep = bt.geometry.centroid.within(boundary)
    result = bt[keep].copy().reset_index(drop=True)
    result["aantal_inwoners"] = result["aantal_inwoners"].clip(lower=0)
    return result


def area_supermarkten_broad(area, margin=None):
    """
    GeoDataFrame with all supermarkets whose centroid falls within `margin` metres
    of the gemeente boundary (EPSG:28992).

    Adds 'lat' and 'lon' columns (WGS84) derived from the point geometry so
    build_viewer_data.py can read them directly.
    """
    if margin is None:
        margin = getattr(_state, "supermarkt_margin", 10000)

    sm = gpd.read_file(_LOCATUS_GPKG_DEFAULT).to_crs(28992)
    boundary = area_boundary(area).buffer(margin)
    keep = sm.geometry.centroid.within(boundary)
    result = sm[keep].copy().reset_index(drop=True)

    # Derive lat/lon from RD geometry for build_viewer_data.py
    wgs84 = result.geometry.to_crs(4326)
    result["lon"] = wgs84.x
    result["lat"] = wgs84.y

    return result


# ---------------------------------------------------------------------------
# PEP 562 — dynamic module attributes resolved from _state at access time
# ---------------------------------------------------------------------------
_DYNAMIC_ATTRS = {
    "DRIVE_MARGIN",
    "WALK_MARGIN",
    "SUPERMARKT_MARGIN",
    "OUT_DIR",
    "BAG_SQLITE",
    "CBS_GPKG",
    "LOCATUS_GPKG",
    "AREAS",
    "NEW_AREAS",
}


def __getattr__(name: str):
    if name not in _DYNAMIC_ATTRS:
        raise AttributeError(f"module 'nl_areas' has no attribute {name!r}")

    if name == "DRIVE_MARGIN":
        return getattr(_state, "drive_margin", 10000)
    if name == "WALK_MARGIN":
        return getattr(_state, "walk_margin", 5000)
    if name == "SUPERMARKT_MARGIN":
        return getattr(_state, "supermarkt_margin", 10000)
    if name == "OUT_DIR":
        return os.environ.get("OUT_DIR", getattr(_state, "out_dir", _OUT_DIR_DEFAULT))
    if name == "BAG_SQLITE":
        return os.environ.get("BAG_SQLITE", _BAG_SQLITE_DEFAULT)
    if name == "CBS_GPKG":
        return _CBS_GPKG_DEFAULT
    if name == "LOCATUS_GPKG":
        return _LOCATUS_GPKG_DEFAULT
    if name in ("AREAS", "NEW_AREAS"):
        return getattr(_state, "areas_list", [])

    raise AttributeError(f"module 'nl_areas' has no attribute {name!r}")
