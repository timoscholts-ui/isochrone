# -*- coding: utf-8 -*-
"""
Export isochrone_output JSONB blobs back to the filesystem for build_heatmaps.py.

Usage:
    python -X utf8 export_from_db.py \\
        --dsn postgresql://iso_worker:changeme@localhost:5432/isochrone_db \\
        --out-dir C:\\Users\\Timo\\Downloads\\isochrone_nl_output

    # Export only specific buurten:
    python -X utf8 export_from_db.py --dsn ... --buurten BU00140000,BU03630000

    # Export all buurten belonging to one or more gemeenten:
    python -X utf8 export_from_db.py --dsn ... --gemeenten GM0363,GM0014

After export, run build_heatmaps.py with:
    $env:OUT_DIR="<out-dir>"
    python -X utf8 build_heatmaps.py

Each buurt gets its own subdirectory: <out-dir>/<slug>/  (slug = buurt_code.lower())
A top-level areas.json manifest is written to <out-dir>/areas.json.
"""

import argparse
import json
import os
import sys

import psycopg2
import psycopg2.extras

# Map isochrone_output column → filename template (slug = buurt_code.lower())
COLUMN_FILE = [
    ("focus_isochrones", "{slug}_focus_isochrones.geojson"),
    ("focus_points",     "{slug}_focus_points.json"),
    ("kpi_lookup",       "{slug}_kpi_lookup.json"),
    ("isochrones_wgs84", "{slug}_isochrones_wgs84.geojson"),
    ("buurten_wgs84",    "{slug}_buurten_wgs84.geojson"),
    ("supermarkten",     "{slug}_supermarkten.json"),
]


def export(dsn: str, out_dir: str,
           buurten: list[str] | None = None,
           gemeenten: list[str] | None = None):
    """
    Export isochrone_output rows to the filesystem.

    buurten:  filter by buurt_code list  (e.g. ["BU00140000"])
    gemeenten: filter by gemeente_code   (e.g. ["GM0014"]) — exports all buurten in those gemeenten
    If neither is given, all rows are exported.
    """
    os.makedirs(out_dir, exist_ok=True)

    conn = psycopg2.connect(dsn)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    if buurten:
        placeholders = ", ".join(["%s"] * len(buurten))
        cur.execute(
            f"SELECT * FROM isochrone_output WHERE buurt_code IN ({placeholders})"
            " ORDER BY gemeente_naam, buurt_naam",
            buurten,
        )
    elif gemeenten:
        placeholders = ", ".join(["%s"] * len(gemeenten))
        cur.execute(
            f"SELECT * FROM isochrone_output WHERE gemeente_code IN ({placeholders})"
            " ORDER BY gemeente_naam, buurt_naam",
            gemeenten,
        )
    else:
        cur.execute(
            "SELECT * FROM isochrone_output ORDER BY gemeente_naam, buurt_naam"
        )

    rows = cur.fetchall()
    cur.close()
    conn.close()

    print(f"Exporting {len(rows)} buurten to {out_dir}")

    manifest = []
    for row in rows:
        slug = row["slug"]          # bu00140000
        buurt_naam = row["buurt_naam"]
        gemeente_naam = row["gemeente_naam"]
        gemeente_code = row["gemeente_code"]

        slug_dir = os.path.join(out_dir, slug)
        os.makedirs(slug_dir, exist_ok=True)

        for col, tpl in COLUMN_FILE:
            data = row[col]
            if data is None:
                continue
            fpath = os.path.join(slug_dir, tpl.format(slug=slug))
            with open(fpath, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)

        print(f"  {gemeente_naam} / {buurt_naam} ({slug})")
        manifest.append({
            "slug": slug,
            "name": buurt_naam,
            "gemeente_naam": gemeente_naam,
            "gemeente_code": gemeente_code,
            "buurt_code": row["buurt_code"],
        })

    manifest_path = os.path.join(out_dir, "areas.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    print(f"\nManifest written: {manifest_path} ({len(manifest)} buurten)")
    print(
        "\nTo run build_heatmaps.py, set:\n"
        f"  $env:OUT_DIR='{out_dir}'\n"
        "  python -X utf8 build_heatmaps.py"
    )

    return manifest


def patch_nl_areas_for_heatmaps(out_dir: str, manifest: list):
    """
    Patch sys.modules so build_heatmaps.py picks up the full buurt list
    via nl_areas.AREAS.

    Call this before importing build_heatmaps if you want to run it from code:
        export_from_db.patch_nl_areas_for_heatmaps(out_dir, manifest)
        import build_heatmaps
        build_heatmaps.main()
    """
    import nl_areas

    area_list = [
        {
            "slug": m["slug"],
            "name": m["name"],
            "gemeente": m["gemeente_naam"],
            "gemeente_code": m["gemeente_code"],
            "buurt_code": m["buurt_code"],
            "spacing": 750,
        }
        for m in manifest
    ]
    nl_areas.set_all_areas(area_list)
    sys.modules["isochrone_areas"] = nl_areas
    os.environ["OUT_DIR"] = out_dir


def main():
    parser = argparse.ArgumentParser(description="Export isochrone_output to filesystem")
    parser.add_argument("--dsn", required=True, help="PostgreSQL DSN")
    parser.add_argument(
        "--out-dir",
        required=True,
        help="Output directory (one subdir per buurt)",
    )
    parser.add_argument(
        "--buurten",
        default=None,
        help="Comma-separated buurt codes, e.g. BU00140000,BU03630000 (default: all)",
    )
    parser.add_argument(
        "--gemeenten",
        default=None,
        help="Comma-separated gemeente codes, e.g. GM0363,GM0014 — exports all buurten in those gemeenten",
    )
    args = parser.parse_args()

    buurt_codes = [c.strip() for c in args.buurten.split(",")] if args.buurten else None
    gemeente_codes = [c.strip() for c in args.gemeenten.split(",")] if args.gemeenten else None
    export(args.dsn, args.out_dir, buurten=buurt_codes, gemeenten=gemeente_codes)


if __name__ == "__main__":
    main()
