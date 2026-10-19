# -*- coding: utf-8 -*-
"""
Stap 3 — KPI-precompute voor de isochrone viewer (alle gebieden).

Voor elke isochroon (focus × mode × minuten) berekenen we:
  - pop_bag  : som van dasymetrische bevolking van BAG-punten binnen (nauwkeurig).
  - wvo      : som van WVO van supermarkten waarvan het punt binnen ligt.
  - buurt_dekking : per buurt de dekkingsfractie (oppervlak isochroon∩buurt / buurt).
  - pop_area : som(buurt_inwoners × dekkingsfractie) — de MAUP-gevoelige schatting.
  - wvo_per_inwoner_bag / _area.

Output per gebied: <slug>_kpi_lookup.json — {focus_id: {mode: {minuten: {...}}}}.
"""

import os
import sys
import json
from collections import defaultdict

from shapely.geometry import Point, shape
from shapely import STRtree, make_valid

import isochrone_areas as areas

# Minimale bevolking (BAG-punten of area-proportioneel) waarvoor de
# WVO-per-inwoner-ratio nog betekenisvol is. Daaronder is de ratio gedomineerd
# door ruis (één of twee adressen) en kan hij uitschieters opleveren van
# honderden m²/inw. Bij te weinig inwoners wordt de ratio op None gezet.
POP_FLOOR = 50


def process_area(area):
    slug = area["slug"]
    print(f"\n[{slug}] {area['name']}")

    iso_path = os.path.join(areas.OUT_DIR, f"{slug}_focus_isochrones.geojson")
    bag_path = os.path.join(areas.OUT_DIR, f"{slug}_bag_adressen.json")

    # --- isochronen ---
    with open(iso_path, encoding="utf-8") as f:
        iso_fc = json.load(f)
    iso_entries = []  # (key, poly)
    for ft in iso_fc["features"]:
        p = ft["properties"]
        # Vereenvoudig de isochroon (25 m) — scheelt veel in de intersectie-tijd
        # met ~500-4000 hoekpunten, met verwaarloosbaar precisieverlies.
        poly = make_valid(shape(ft["geometry"])).simplify(25, preserve_topology=True)
        key = (p["focus_id"], p["mode"], str(p["minuten"]))
        iso_entries.append((key, poly))
    print(f"  isochronen geladen: {len(iso_entries)}")

    # --- BAG adressen ---
    with open(bag_path, encoding="utf-8") as f:
        bag = json.load(f)
    print(f"  BAG adressen geladen: {len(bag)}")

    # --- supermarkten (cross-regio: elke supermarkt die een isochroon bereikt) ---
    sm = areas.area_supermarkten_broad(area)
    supermarkten = [{"x": r.geometry.centroid.x, "y": r.geometry.centroid.y,
                     "wvo": int(r["WVO"] or 0), "naam": r["NAAM"]}
                    for _, r in sm.iterrows()]
    print(f"  supermarkten ({len(supermarkten)}): "
          f"{[s['naam'] for s in supermarkten]}")

    # --- CBS buurten (cross-regio) ---
    buurten = areas.area_buurten_broad(area)
    buurt_geoms = [make_valid(g) for g in buurten.geometry]
    buurt_codes = list(buurten["buurtcode"])
    inw_by_buurt = dict(zip(buurt_codes, [max(0, int(w)) for w in buurten["aantal_inwoners"]]))  # -99997 (water) → 0
    buurt_areas = [g.area for g in buurt_geoms]

    # --- pop_bag + wvo (inverted: per isochroon de punten opzoeken) ---
    bag_points = [Point(a["x"], a["y"]) for a in bag]
    bag_tree = STRtree(bag_points)
    sm_points = [Point(s["x"], s["y"]) for s in supermarkten]
    sm_tree = STRtree(sm_points)

    pop_bag = {}
    wvo = {}
    for key, poly in iso_entries:
        pb = 0.0
        for i in bag_tree.query(poly, predicate="contains"):
            pb += bag[i]["pop"]
        pop_bag[key] = pb
        wv = 0.0
        for i in sm_tree.query(poly, predicate="contains"):
            wv += supermarkten[i]["wvo"]
        wvo[key] = wv
    print("  pop_bag + wvo berekend")

    # --- buurt-dekking + pop_area ---
    buurt_tree = STRtree(buurt_geoms)
    dekking_map = {}
    pop_area = {}
    for key, poly in iso_entries:
        dek = {}
        for idx in buurt_tree.query(poly, predicate="intersects"):
            inter = poly.intersection(buurt_geoms[idx]).area
            if inter > 0:
                dek[buurt_codes[idx]] = inter / buurt_areas[idx]
        dekking_map[key] = dek
        pop_area[key] = sum(inw_by_buurt[bc] * frac for bc, frac in dek.items())

    # --- KPI-lookup opbouwen ---
    kpi = {}
    for key, poly in iso_entries:
        focus_id, mode, minuten = key
        pb = pop_bag.get(key, 0.0)
        pa = pop_area.get(key, 0.0)
        wv = wvo.get(key, 0.0)
        dek = {bc: round(frac, 4) for bc, frac in dekking_map[key].items()}
        kpi.setdefault(focus_id, {}).setdefault(mode, {})[minuten] = {
            "pop_bag": round(pb, 1),
            "pop_area": round(pa, 1),
            "wvo": round(wv, 1),
            "wvo_per_inwoner_bag": round(wv / pb, 4) if pb >= POP_FLOOR else None,
            "wvo_per_inwoner_area": round(wv / pa, 4) if pa >= POP_FLOOR else None,
            "buurt_dekking": dek,
        }

    out_kpi = os.path.join(areas.OUT_DIR, f"{slug}_kpi_lookup.json")
    with open(out_kpi, "w", encoding="utf-8") as f:
        json.dump(kpi, f)
    print(f"  geschreven: {os.path.basename(out_kpi)} "
          f"({os.path.getsize(out_kpi)/1e6:.2f} MB)")

    # --- sanity check op één focuspunt ---
    f0 = sorted(kpi.keys())[0]
    print(f"  sanity focus {f0}:")
    for mode in ["auto", "fiets", "lopen"]:
        row = []
        for m in ["5", "10", "15", "20"]:
            v = kpi[f0][mode][m]
            row.append(f"{m}min: pop_bag={v['pop_bag']:.0f} wvo={v['wvo']:.0f}")
        print(f"    {mode}: {' | '.join(row)}")


def main():
    os.makedirs(areas.OUT_DIR, exist_ok=True)
    slugs = sys.argv[1:] or [a["slug"] for a in areas.NEW_AREAS]
    for slug in slugs:
        try:
            process_area(areas.get_area(slug))
        except Exception as e:  # noqa: BLE001 — doorgaan met de volgende gebieden
            print(f"\n!! [{slug}] mislukt: {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
