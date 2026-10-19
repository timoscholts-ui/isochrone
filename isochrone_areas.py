# -*- coding: utf-8 -*-
"""
Gebiedsconfiguratie voor de isochrone supermarkt-analyse.

Studiegebied: een selectie van buurten uit CBS 2023
(`CBS_Hoogvliet_2023 (1).geojson`), gegroepeerd in 6 gemeente-gebieden. De
gebiedsgrens is de unie van de geselecteerde buurten; netwerk, supermarkten en
BAG-punten worden op die grens (+ marge) geclipt.

De cross-regio buurten (voor BAG-koppeling) en supermarkten komen nog steeds uit
de CBS/Locatus-bronnen, maar gefilterd op de nieuwe grens.
"""

import os

import geopandas as gpd
import pandas as pd
from shapely.ops import unary_union

# Datapaden: standaard de lokale (Windows) paden; overschrijfbaar via env-vars
# voor een server-deployment (zie requirements.txt + Procfile).
CBS_BUURTEN_GEOJSON = os.environ.get(
    "CBS_BUURTEN_GEOJSON",
    r"C:\Users\Timo\Downloads\CBS_Hoogvliet_2023 (1).geojson",
)
CBS_GPKG = os.environ.get(
    "CBS_GPKG",
    r"C:\Users\Timo\Downloads\WijkBuurtkaart_2025_v1\WijkBuurtkaart_2025_v1\wijkenbuurten_2025_v1.gpkg",
)
LOCATUS_GPKG = os.environ.get(
    "LOCATUS_GPKG", r"C:\Users\Timo\Downloads\Supermarkt_polygonen.gpkg"
)
BAG_SQLITE = os.environ.get(
    "BAG_SQLITE", r"C:\Users\Timo\Desktop\Claude map\Funda Pipline New\bag\bag.sqlite"
)

# Alle outputs (RD-tussenproducten + WGS84-viewerbestanden) komen hier terecht.
OUT_DIR = os.environ.get("OUT_DIR", r"C:\Users\Timo\Downloads\isochrone_viewer")

# 6 gemeente-gebieden. De buurten worden per gemeente uit de geojson gehaald
# (kolom `Gemeentenaam`).
AREAS = [
    {"slug": "bunnik", "name": "Bunnik", "gemeente": "Bunnik", "spacing": 750},
    {"slug": "de_bilt", "name": "De Bilt", "gemeente": "De Bilt", "spacing": 1000},
    {"slug": "houten", "name": "Houten", "gemeente": "Houten", "spacing": 1000},
    {"slug": "rhenen", "name": "Rhenen", "gemeente": "Rhenen", "spacing": 1000},
    {"slug": "utrecht", "name": "Utrecht", "gemeente": "Utrecht", "spacing": 1000},
    {"slug": "wijk_bij_duurstede", "name": "Wijk bij Duurstede", "gemeente": "Wijk bij Duurstede", "spacing": 750},
]

# Gebieden waarvoor we daadwerkelijk open-source isochronen bouwen.
NEW_AREAS = AREAS

# Netwerk-buffers rondom het gebied (meter). De drive-isochroon reikt ~10 km
# (auto); bewust kleiner dan de 20-min-reikwijdte zodat de buitenste isochronen
# op de grens worden afgekapt en het netwerk kleiner blijft.
DRIVE_MARGIN = 10000
WALK_MARGIN = 5000
# Supermarkten worden cross-regio geteld: elk isochroon telt elke supermarkt die
# het daadwerkelijk bereikt, dus een marge gelijk aan de drive-reikwijdte.
SUPERMARKT_MARGIN = DRIVE_MARGIN


def get_area(slug):
    for a in AREAS:
        if a["slug"] == slug:
            return a
    raise KeyError(f"Onbekend gebied: {slug}")


# --- CBS 2023 buurten (studiegebied) ----------------------------------------
_study_cache = None


def _study_buurten():
    """GeoDataFrame (EPSG:28992) van de 100 geselecteerde buurten."""
    global _study_cache
    if _study_cache is None:
        gdf = gpd.read_file(CBS_BUURTEN_GEOJSON).to_crs(28992)
        gdf = gdf.rename(
            columns={
                "BUCODE": "buurtcode",
                "Buurtnaam": "buurtnaam",
                "AantalInwoners": "aantal_inwoners",
            }
        )
        gdf["aantal_inwoners"] = (
            pd.to_numeric(gdf["aantal_inwoners"], errors="coerce").fillna(0).astype(int)
        )
        gdf = gdf[["buurtcode", "buurtnaam", "aantal_inwoners", "Gemeentenaam", "geometry"]]
        _study_cache = gdf
    return _study_cache


def area_buurten(area):
    """GeoDataFrame met de geselecteerde buurten van het gebied (buurtcode,
    buurtnaam, aantal_inwoners)."""
    gdf = _study_buurten()
    return gdf[gdf["Gemeentenaam"] == area["gemeente"]].reset_index(drop=True)


def area_boundary(area):
    """Shapely-geometrie (EPSG:28992) van de unie van de geselecteerde buurten."""
    return unary_union(area_buurten(area).geometry.tolist())


# --- CBS volledige buurten (cross-regio) ------------------------------------
_cbs_cache = {}


def _cbs(layer):
    if layer not in _cbs_cache:
        _cbs_cache[layer] = gpd.read_file(CBS_GPKG, layer=layer)
    return _cbs_cache[layer]


def area_buurten_broad(area, margin=SUPERMARKT_MARGIN):
    """GeoDataFrame met alle buurten waarvan het centroïde binnen `margin`
    meter van de nieuwe grens ligt — kruist gemeentegrenzen, zodat de BAG-koppeling
    en de area-proportionele bevolking cross-regio zijn."""
    bt = _cbs("buurten")
    boundary = area_boundary(area).buffer(margin)
    keep = bt.geometry.centroid.within(boundary)
    return bt[keep].reset_index(drop=True)


def area_supermarkten_broad(area, margin=SUPERMARKT_MARGIN):
    """GeoDataFrame met alle supermarkten waarvan het centroïde binnen
    `margin` meter van de nieuwe grens ligt (EPSG:28992)."""
    sm = gpd.read_file(LOCATUS_GPKG)
    boundary = area_boundary(area).buffer(margin)
    keep = sm.geometry.centroid.within(boundary)
    return sm[keep].reset_index(drop=True)
