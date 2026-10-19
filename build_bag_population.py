# -*- coding: utf-8 -*-
"""
Stap 2 — BAG-woningadressen + dasymetrische bevolking per adres (alle gebieden).

Leest verblijfsobjecten met 'woonfunctie' uit de nationale bag.sqlite via een
ruimtelijke bbox-filter over de cross-regio buurten (15 km marge), koppelt elk
adres aan een buurt, en verdeelt de CBS-buurtinwoners gelijkmatig over de
woonadressen in die buurt (pop_per_adres = buurt_inwoners / aantal_woonadressen).

Output per gebied: <slug>_bag_adressen.json — list van {x, y (RD), pop, buurtcode}.
"""

import os
import sys
import json
import sqlite3

from shapely.geometry import Point
from shapely import STRtree

import isochrone_areas as areas


def process_area(area):
    slug = area["slug"]
    buurten = areas.area_buurten_broad(area)
    print(f"\n[{slug}] {area['name']} — {len(buurten)} buurten (cross-regio)")

    minx, miny, maxx, maxy = buurten.total_bounds

    # --- BAG verblijfsobjecten met woonfunctie binnen bbox ---
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
    print(f"  BAG woonadressen in bbox: {len(rows)}")

    # --- koppel aan buurt (cross-regio) ---
    buurt_geoms = list(buurten.geometry)
    buurt_codes = list(buurten["buurtcode"])
    buurt_inw = [max(0, int(w)) for w in buurten["aantal_inwoners"]]  # -99997 (water) → 0
    tree = STRtree(buurt_geoms)

    adressen = []       # {x, y, buurtcode}
    outside = 0
    for (x, y) in rows:
        p = Point(x, y)
        buurtcode = None
        for idx in sorted(tree.query(p)):
            if buurt_geoms[idx].contains(p):
                buurtcode = buurt_codes[idx]
                break
        if buurtcode is None:
            outside += 1
            continue
        adressen.append({"x": round(x, 1), "y": round(y, 1), "buurtcode": buurtcode})

    print(f"  BAG adressen in brede regio: {len(adressen)} (buiten buurten: {outside})")

    # --- pop per adres ---
    inw_by_buurt = dict(zip(buurt_codes, buurt_inw))
    n_by_buurt = {}
    for a in adressen:
        n_by_buurt[a["buurtcode"]] = n_by_buurt.get(a["buurtcode"], 0) + 1

    for a in adressen:
        bc = a["buurtcode"]
        n = n_by_buurt.get(bc, 0)
        a["pop"] = (inw_by_buurt.get(bc, 0) / n) if n else 0.0

    totaal_pop = sum(a["pop"] for a in adressen)
    print(f"  Som pop (adressen) = {totaal_pop:.0f} "
          f"(CBS buurttotaal ≈ {sum(buurt_inw)})")

    out_json = os.path.join(areas.OUT_DIR, f"{slug}_bag_adressen.json")
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(adressen, f)
    print(f"  geschreven: {os.path.basename(out_json)} "
          f"({len(adressen)} adressen, {os.path.getsize(out_json)/1e6:.2f} MB)")


def main():
    os.makedirs(areas.OUT_DIR, exist_ok=True)
    slugs = sys.argv[1:] or [a["slug"] for a in areas.NEW_AREAS]
    for slug in slugs:
        process_area(areas.get_area(slug))


if __name__ == "__main__":
    main()
