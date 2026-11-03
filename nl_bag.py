# -*- coding: utf-8 -*-
"""
BAG address loading for the NL-wide isochrone pipeline.

Replacement for build_bag_population.process_area() that reads from
the pcodes-sqlite bag-export.sqlite (alllabel view).

Key differences from the original build_bag_population.py:
  - Source: alllabel view (JOIN of vbos + nums + address tables)
  - Filter: gebruiksdoelen contains "woonfunctie" or "wonen" (JSON array match)
  - Status: vbo_status column (not status)
  - 0-inhabitants override: if a buurt has 0 CBS inhabitants,
    every woon-address in that buurt is counted as 2 inhabitants

Output: same format as build_bag_population.py:
  <slug>_bag_adressen.json  ->  [{x, y, pop, buurtcode}, ...]
"""

import json
import os
import sqlite3
from collections import Counter
from pathlib import Path

import isochrone_areas as areas  # will be nl_areas via monkey-patch
from shapely.geometry import Point
from shapely import STRtree


BAG_SQLITE = os.environ.get(
    "BAG_SQLITE",
    r"C:\Users\Timo\Downloads\pcodes-sqlite\bag-export.sqlite",
)

# gebruiksdoelen filter: JSON array must contain "woonfunctie" or "wonen"
_WONEN_SQL = (
    "(gebruiksdoelen LIKE '%\"woonfunctie\"%' OR gebruiksdoelen LIKE '%\"wonen\"%')"
)


def process_area(area: dict) -> None:
    """
    Load BAG woon-adressen for the area, assign to buurten,
    compute dasymetrische bevolking, write <slug>_bag_adressen.json.
    """
    slug = area["slug"]
    buurten = areas.area_buurten_broad(area)
    print(f"\n[{slug}] {area['name']} — {len(buurten)} buurten (cross-regio)")

    # Bounding box over the cross-regional buurten
    minx, miny, maxx, maxy = buurten.total_bounds

    # Query BAG
    bag_path = os.environ.get("BAG_SQLITE", BAG_SQLITE)
    con = sqlite3.connect(bag_path)
    cur = con.cursor()
    cur.execute(
        f"""
        SELECT x, y FROM alllabel
        WHERE x BETWEEN ? AND ? AND y BETWEEN ? AND ?
          AND {_WONEN_SQL}
          AND vbo_status NOT LIKE '%ingetrokken%'
        """,
        (minx, maxx, miny, maxy),
    )
    rows = cur.fetchall()
    con.close()
    print(f"  BAG woonadressen in bbox: {len(rows)}")

    # Assign each address to a buurt via STRtree
    buurt_geoms = list(buurten.geometry)
    buurt_codes = list(buurten["buurtcode"])
    buurt_inwoners = {
        code: max(0, int(inw))
        for code, inw in zip(buurt_codes, buurten["aantal_inwoners"])
    }
    tree = STRtree(buurt_geoms)

    addr_buurt: list[tuple[float, float, str]] = []  # (x, y, buurtcode)
    outside = 0
    for (x, y) in rows:
        if x is None or y is None:
            outside += 1
            continue
        p = Point(x, y)
        buurtcode = None
        for idx in sorted(tree.query(p)):
            if buurt_geoms[idx].contains(p):
                buurtcode = buurt_codes[idx]
                break
        if buurtcode is None:
            outside += 1
        else:
            addr_buurt.append((x, y, buurtcode))

    print(f"  Toegewezen: {len(addr_buurt)}  buiten buurten: {outside}")

    # Count addresses per buurt
    n_per_buurt = Counter(bc for _, _, bc in addr_buurt)

    # Compute pop_per_adres per buurt
    # Override: buurt with 0 CBS inhabitants → 2 per address
    pop_per_adres: dict[str, float] = {}
    for code in buurt_codes:
        n = n_per_buurt.get(code, 0)
        inw = buurt_inwoners.get(code, 0)
        if n == 0:
            pop_per_adres[code] = 0.0
        elif inw == 0:
            # CBS reports 0 inhabitants — fall back to 2 per woon-address
            pop_per_adres[code] = 2.0
        else:
            pop_per_adres[code] = inw / n

    # Build output list
    adressen = [
        {"x": x, "y": y, "pop": pop_per_adres.get(bc, 0.0), "buurtcode": bc}
        for x, y, bc in addr_buurt
    ]

    out_json = os.path.join(areas.OUT_DIR, f"{slug}_bag_adressen.json")
    os.makedirs(areas.OUT_DIR, exist_ok=True)
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(adressen, f)
    print(f"  Geschreven: {os.path.basename(out_json)} ({len(adressen)} adressen)")
