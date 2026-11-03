# -*- coding: utf-8 -*-
"""
NL-wide isochrone worker.

Claims one job at a time from isochrone_jobs, runs the full pipeline,
stores 6 output files as JSONB in isochrone_output, then moves to the next job.

Usage:
    python -X utf8 nl_worker.py \\
        --dsn postgresql://iso_worker:changeme@localhost:5432/isochrone_db \\
        --max-ram 8 \\
        --worker-id laptop-arjan
        [--once]   # exit after one job (for testing)

The worker calls reset_stale_jobs() at startup to reclaim crashed jobs.
"""

import argparse
import gc
import importlib
import json
import os
import shutil
import socket
import sys
import tempfile
import traceback
from pathlib import Path

import psycopg2
import psycopg2.extras

# ---------------------------------------------------------------------------
# Margin tiers (drive_km, walk_km) — 5 levels, largest first
# ---------------------------------------------------------------------------
MARGIN_TIERS = [
    (10000, 5000),
    (8000, 4000),
    (6000, 3000),
    (5000, 2500),
    (3000, 1500),
]

# Output files written by the pipeline per gemeente slug
OUTPUT_FILES = [
    ("{slug}_focus_isochrones.geojson", "focus_isochrones"),
    ("{slug}_focus_points.json",        "focus_points"),
    ("{slug}_kpi_lookup.json",          "kpi_lookup"),
    ("{slug}_isochrones_wgs84.geojson", "isochrones_wgs84"),
    ("{slug}_buurten_wgs84.geojson",    "buurten_wgs84"),
    ("{slug}_supermarkten.json",        "supermarkten"),
]


def pick_margin_tier(est_ram_gb_10km: float, max_ram: float):
    """
    Select the largest margin tier where the estimated RAM fits within
    85% of max_ram.  Returns (drive_margin, walk_margin).

    RAM scales as (drive_km/10)² relative to the 10 km baseline.
    """
    for drive_m, walk_m in MARGIN_TIERS:
        scale = (drive_m / 10000) ** 2
        est = (est_ram_gb_10km - 1.0) * scale + 1.0
        if est <= max_ram * 0.85:
            return drive_m, walk_m
    # Nothing fits — use smallest tier and hope for the best
    return MARGIN_TIERS[-1]


def ram_ceiling_for_claim(est_ram_gb_10km: float, max_ram: float) -> float:
    """
    RAM estimate at tier 4 (5 km drive) — used as the upper bound for
    the SELECT query so workers don't claim jobs they definitely can't run.
    """
    drive_m, _ = MARGIN_TIERS[3]  # tier index 3 = (5000, 2500)
    scale = (drive_m / 10000) ** 2
    return (est_ram_gb_10km - 1.0) * scale + 1.0


def claim_job(cur, worker_id: str, max_ram: float):
    """
    Claim the next available job.  Returns the job row dict or None.
    Uses SELECT FOR UPDATE SKIP LOCKED for safe concurrent access.
    """
    # We claim jobs whose est_ram_gb (at tier-4 margins) fits our max_ram.
    # The subquery picks the heaviest job that still fits (prioritise large jobs
    # so small workers don't gobble them all up).
    cur.execute(
        """
        UPDATE isochrone_jobs
           SET status        = 'running',
               worker_id     = %s,
               started_at    = NOW(),
               attempt_count = attempt_count + 1
         WHERE id = (
            SELECT id FROM isochrone_jobs
             WHERE status = 'queued'
               AND attempt_count < 3
             ORDER BY est_ram_gb DESC
             LIMIT 1
             FOR UPDATE SKIP LOCKED
         )
         RETURNING *
        """,
        (worker_id,),
    )
    row = cur.fetchone()
    if row is None:
        return None
    cols = [desc[0] for desc in cur.description]
    return dict(zip(cols, row))


def mark_done(cur, buurt_code: str):
    cur.execute(
        "UPDATE isochrone_jobs SET status='done', finished_at=NOW() WHERE buurt_code=%s",
        (buurt_code,),
    )


def mark_failed(cur, buurt_code: str, error_msg: str):
    cur.execute(
        """UPDATE isochrone_jobs
              SET status      = 'failed',
                  finished_at = NOW(),
                  error_msg   = %s
            WHERE buurt_code = %s""",
        (error_msg[:4000], buurt_code),
    )


def requeue_job(conn, buurt_code: str):
    """Re-queue a job that was interrupted (e.g. KeyboardInterrupt)."""
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE isochrone_jobs
                  SET status     = 'queued',
                      worker_id  = NULL,
                      started_at = NULL
                WHERE buurt_code = %s AND status = 'running'""",
            (buurt_code,),
        )
    conn.commit()


def store_output(cur, job: dict, slug: str, files: dict,
                 drive_margin: int, walk_margin: int):
    """Insert/update isochrone_output with JSONB blobs."""
    cur.execute(
        """
        INSERT INTO isochrone_output
            (buurt_code, buurt_naam, gemeente_code, gemeente_naam, slug,
             focus_isochrones, focus_points, kpi_lookup,
             isochrones_wgs84, buurten_wgs84, supermarkten,
             drive_margin_used, walk_margin_used, stored_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
        ON CONFLICT (buurt_code) DO UPDATE SET
            buurt_naam        = EXCLUDED.buurt_naam,
            gemeente_code     = EXCLUDED.gemeente_code,
            gemeente_naam     = EXCLUDED.gemeente_naam,
            slug              = EXCLUDED.slug,
            focus_isochrones  = EXCLUDED.focus_isochrones,
            focus_points      = EXCLUDED.focus_points,
            kpi_lookup        = EXCLUDED.kpi_lookup,
            isochrones_wgs84  = EXCLUDED.isochrones_wgs84,
            buurten_wgs84     = EXCLUDED.buurten_wgs84,
            supermarkten      = EXCLUDED.supermarkten,
            drive_margin_used = EXCLUDED.drive_margin_used,
            walk_margin_used  = EXCLUDED.walk_margin_used,
            stored_at         = NOW()
        """,
        (
            job["buurt_code"],
            job["buurt_naam"],
            job["gemeente_code"],
            job["gemeente_naam"],
            slug,
            psycopg2.extras.Json(files.get("focus_isochrones")),
            psycopg2.extras.Json(files.get("focus_points")),
            psycopg2.extras.Json(files.get("kpi_lookup")),
            psycopg2.extras.Json(files.get("isochrones_wgs84")),
            psycopg2.extras.Json(files.get("buurten_wgs84")),
            psycopg2.extras.Json(files.get("supermarkten")),
            drive_margin,
            walk_margin,
        ),
    )


def run_pipeline(job: dict, drive_margin: int, walk_margin: int, temp_dir: str):
    """
    Monkey-patch isochrone_areas, reload build modules, run the 4 pipeline steps,
    return a dict of {column_name: parsed_json}.
    """
    import nl_areas

    buurt_code   = job["buurt_code"]
    buurt_naam   = job["buurt_naam"]
    gemeente_code = job["gemeente_code"]
    gemeente_naam = job["gemeente_naam"]
    spacing      = job["spacing"]

    # 1. Configure nl_areas for this buurt
    area = nl_areas.set_current_area(
        buurt_code, drive_margin, walk_margin,
        buurt_naam, gemeente_code, gemeente_naam, spacing
    )
    slug = area["slug"]

    # 2. Monkey-patch sys.modules so `import isochrone_areas` resolves to nl_areas
    sys.modules["isochrone_areas"] = nl_areas

    # 3. Set OUT_DIR env so all build scripts write to a temp directory
    os.environ["OUT_DIR"] = temp_dir
    os.makedirs(temp_dir, exist_ok=True)

    # 4. Set BAG path for nl_bag (pcodes-sqlite)
    os.environ.setdefault(
        "BAG_SQLITE",
        r"C:\Users\Timo\Downloads\pcodes-sqlite\bag-export.sqlite",
    )

    # 5. Reload build modules so they re-import isochrone_areas (= nl_areas)
    #    and re-execute module-level code (picks up DRIVE_MARGIN / WALK_MARGIN).
    import nl_bag
    import build_focus_isochrones
    import build_kpi_lookup
    import build_viewer_data

    importlib.reload(nl_bag)
    importlib.reload(build_focus_isochrones)
    importlib.reload(build_kpi_lookup)
    importlib.reload(build_viewer_data)

    # 6. Run the pipeline
    print(f"\n[{gemeente_code}] {gemeente_naam}  drive={drive_margin}m walk={walk_margin}m")
    nl_bag.process_area(area)          # uses alllabel view + 0-inw override
    build_focus_isochrones.process_area(area)
    build_kpi_lookup.process_area(area)
    build_viewer_data.process_area(area)

    # 7. Read output files into memory
    result = {}
    for tpl, col in OUTPUT_FILES:
        filename = tpl.format(slug=slug)
        fpath = os.path.join(temp_dir, filename)
        if os.path.exists(fpath):
            with open(fpath, encoding="utf-8") as f:
                result[col] = json.load(f)
        else:
            print(f"  WARNING: expected output file not found: {filename}")
            result[col] = None

    return result


def work_loop(dsn: str, max_ram: float, worker_id: str, once: bool = False):
    conn = psycopg2.connect(dsn)
    conn.autocommit = False

    # Reset stale jobs once at startup
    with conn.cursor() as cur:
        cur.execute("SELECT reset_stale_jobs()")
        n = cur.fetchone()[0]
        conn.commit()
        if n:
            print(f"Reset {n} stale jobs.")

    current_code = None

    try:
        while True:
            with conn.cursor() as cur:
                job = claim_job(cur, worker_id, max_ram)
                conn.commit()

            if job is None:
                print("No jobs available. Exiting.")
                break

            current_code = job["buurt_code"]
            drive_margin, walk_margin = pick_margin_tier(job["est_ram_gb"], max_ram)

            # Create a temp dir for this job's output files
            temp_dir = os.path.join(
                tempfile.gettempdir(), "isochrone_nl", job["buurt_code"]
            )
            os.makedirs(temp_dir, exist_ok=True)

            try:
                files = run_pipeline(job, drive_margin, walk_margin, temp_dir)
                slug = job["buurt_code"].lower()

                with conn.cursor() as cur:
                    store_output(cur, job, slug, files, drive_margin, walk_margin)
                    mark_done(cur, job["buurt_code"])
                    conn.commit()

                print(f"  [DONE] {job['buurt_naam']} ({job['gemeente_naam']}) stored.")

            except Exception as exc:
                conn.rollback()
                tb = traceback.format_exc()
                print(f"  [FAILED] {job['buurt_naam']}: {exc}\n{tb}")
                with conn.cursor() as cur:
                    mark_failed(cur, job["buurt_code"], tb)
                    conn.commit()

            finally:
                # Clean up temp files unless ISO_KEEP_FILES=1
                if os.environ.get("ISO_KEEP_FILES") != "1":
                    shutil.rmtree(temp_dir, ignore_errors=True)
                gc.collect()
                current_code = None

            if once:
                break

    except KeyboardInterrupt:
        print("\nInterrupted. Re-queuing current job...")
        if current_code:
            requeue_job(conn, current_code)
        print("Done.")

    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description="NL-wide isochrone worker")
    parser.add_argument("--dsn", required=True, help="PostgreSQL DSN")
    parser.add_argument(
        "--max-ram",
        type=float,
        default=8.0,
        help="Maximum RAM in GB this worker may use (default: 8)",
    )
    parser.add_argument(
        "--worker-id",
        default=socket.gethostname(),
        help="Unique worker identifier (default: hostname)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Process one job then exit (for testing)",
    )
    args = parser.parse_args()

    work_loop(args.dsn, args.max_ram, args.worker_id, once=args.once)


if __name__ == "__main__":
    main()
