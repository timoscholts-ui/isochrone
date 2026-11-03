# -*- coding: utf-8 -*-
"""
Terminal progress dashboard for the NL-wide isochrone pipeline.

Usage:
    python -X utf8 monitor.py --dsn postgresql://iso_worker:changeme@localhost:5432/isochrone_db
    python -X utf8 monitor.py --dsn ... --refresh 30 --auto-reset
"""

import argparse
import sys
import time
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras

# ANSI escape codes
CLEAR = "\033[2J\033[H"
BOLD = "\033[1m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
CYAN = "\033[36m"
RESET = "\033[0m"

BAR_WIDTH = 40


def progress_bar(done: int, total: int) -> str:
    frac = done / total if total else 0
    filled = int(BAR_WIDTH * frac)
    bar = "█" * filled + "░" * (BAR_WIDTH - filled)
    return f"[{bar}] {frac*100:.1f}%"


def fetch_stats(conn):
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # Buurt counts by status
    cur.execute(
        """
        SELECT status,
               COUNT(*)                    AS buurt_count,
               COUNT(DISTINCT gemeente_code) AS gemeente_count
          FROM isochrone_jobs
         GROUP BY status
        """
    )
    rows = cur.fetchall()
    counts       = {r["status"]: r["buurt_count"]    for r in rows}
    gem_counts   = {r["status"]: r["gemeente_count"] for r in rows}

    # Total gemeente count (for the "X/342 gemeenten" line)
    cur.execute("SELECT COUNT(DISTINCT gemeente_code) FROM isochrone_jobs")
    total_gemeenten = cur.fetchone()["count"]

    # Completed gemeenten: all buurten done (none queued/running/failed)
    cur.execute(
        """
        SELECT COUNT(*) FROM (
            SELECT gemeente_code
              FROM isochrone_jobs
             GROUP BY gemeente_code
            HAVING COUNT(*) FILTER (WHERE status IN ('queued','running','failed')) = 0
               AND COUNT(*) FILTER (WHERE status = 'done') > 0
        ) g
        """
    )
    gemeenten_done = cur.fetchone()["count"]

    # Per-worker active jobs
    cur.execute(
        """
        SELECT worker_id, buurt_naam, gemeente_naam, started_at, est_ram_gb
          FROM isochrone_jobs
         WHERE status = 'running'
         ORDER BY started_at
        """
    )
    running = cur.fetchall()

    # Average duration of completed jobs (minutes)
    cur.execute(
        """
        SELECT AVG(EXTRACT(EPOCH FROM (finished_at - started_at)) / 60)
          FROM isochrone_jobs
         WHERE status = 'done' AND finished_at IS NOT NULL
        """
    )
    avg_min = cur.fetchone()["avg"]

    # Per-gemeente progress breakdown (top incomplete gemeenten)
    cur.execute(
        """
        SELECT gemeente_naam,
               COUNT(*)                                    AS total,
               COUNT(*) FILTER (WHERE status = 'done')    AS done,
               COUNT(*) FILTER (WHERE status = 'running') AS running,
               COUNT(*) FILTER (WHERE status = 'failed')  AS failed
          FROM isochrone_jobs
         GROUP BY gemeente_naam
        HAVING COUNT(*) FILTER (WHERE status IN ('queued','running','failed')) > 0
         ORDER BY done DESC, total DESC
         LIMIT 8
        """
    )
    gemeente_progress = cur.fetchall()

    # Recent completions (last 8)
    cur.execute(
        """
        SELECT buurt_naam, gemeente_naam, finished_at,
               EXTRACT(EPOCH FROM (finished_at - started_at)) / 60 AS dur_min
          FROM isochrone_jobs
         WHERE status = 'done'
         ORDER BY finished_at DESC
         LIMIT 8
        """
    )
    recent_done = cur.fetchall()

    # Recent failures (last 5)
    cur.execute(
        """
        SELECT buurt_naam, gemeente_naam, finished_at, attempt_count,
               LEFT(error_msg, 120) AS err
          FROM isochrone_jobs
         WHERE status = 'failed'
         ORDER BY finished_at DESC NULLS LAST
         LIMIT 5
        """
    )
    recent_failed = cur.fetchall()

    # Stale running jobs (> 4h)
    cur.execute(
        """
        SELECT buurt_naam, gemeente_naam, worker_id, started_at
          FROM isochrone_jobs
         WHERE status = 'running'
           AND started_at < NOW() - INTERVAL '4 hours'
        """
    )
    stale = cur.fetchall()

    cur.close()
    return (counts, gem_counts, total_gemeenten, gemeenten_done,
            running, avg_min, gemeente_progress, recent_done, recent_failed, stale)


def render(counts, gem_counts, total_gemeenten, gemeenten_done,
           running, avg_min, gemeente_progress, recent_done, recent_failed, stale,
           refresh):
    total_buurten = sum(counts.values())
    done    = counts.get("done", 0)
    failed  = counts.get("failed", 0)
    running_n = counts.get("running", 0)
    queued  = counts.get("queued", 0)

    now = datetime.now(timezone.utc)
    lines = []
    lines.append(
        f"{BOLD}NL Isochrone Pipeline Monitor{RESET}  "
        f"{now.strftime('%Y-%m-%d %H:%M:%S UTC')}  (refresh {refresh}s)"
    )
    lines.append("─" * 70)

    # Buurt progress bar
    lines.append(progress_bar(done, total_buurten) + f"  {done}/{total_buurten} buurten")
    lines.append(
        f"  {GREEN}done: {done}{RESET}  "
        f"{CYAN}running: {running_n}{RESET}  "
        f"queued: {queued}  "
        f"{RED}failed: {failed}{RESET}"
    )

    # Gemeente summary line
    lines.append(
        f"  gemeenten volledig klaar: {GREEN}{gemeenten_done}{RESET}/{total_gemeenten}  "
        f"(gedeeltelijk: {gem_counts.get('done', 0)} gem. hebben ≥1 buurt done)"
    )

    # ETA
    if avg_min and done > 0 and queued > 0:
        workers = max(running_n, 1)
        eta_min = (queued / workers) * avg_min
        h, m = divmod(int(eta_min), 60)
        lines.append(
            f"  avg buurt duration: {avg_min:.1f} min  "
            f"ETA: {h}h {m}m  (workers: {workers})"
        )

    lines.append("")

    # Active workers
    if running:
        lines.append(f"{BOLD}Active workers:{RESET}")
        for r in running:
            elapsed = (now - r["started_at"].replace(tzinfo=timezone.utc)).total_seconds() / 60
            lines.append(
                f"  {r['worker_id']:20s}  "
                f"{r['gemeente_naam']:20s} / {r['buurt_naam']:25s}  "
                f"{elapsed:.0f} min  (~{r['est_ram_gb']:.1f} GB)"
            )
        lines.append("")

    # Gemeente-level breakdown (incomplete gemeenten)
    if gemeente_progress:
        lines.append(f"{BOLD}Gemeenten in progress:{RESET}")
        for g in gemeente_progress:
            bar_w = 20
            frac = g["done"] / g["total"] if g["total"] else 0
            filled = int(bar_w * frac)
            mini_bar = "█" * filled + "░" * (bar_w - filled)
            fail_str = f"  {RED}{g['failed']} failed{RESET}" if g["failed"] else ""
            run_str  = f"  {CYAN}{g['running']} running{RESET}" if g["running"] else ""
            lines.append(
                f"  [{mini_bar}] {g['done']:3d}/{g['total']:3d}  "
                f"{g['gemeente_naam']}{run_str}{fail_str}"
            )
        lines.append("")

    # Stale warnings
    if stale:
        lines.append(f"{YELLOW}STALE JOBS (> 4h):{RESET}")
        for s in stale:
            lines.append(
                f"  {s['gemeente_naam']} / {s['buurt_naam']} on {s['worker_id']}"
            )
        lines.append("")

    # Recent completions
    if recent_done:
        lines.append(f"{BOLD}Recently completed:{RESET}")
        for r in recent_done:
            ts = r["finished_at"].strftime("%H:%M") if r["finished_at"] else "?"
            lines.append(
                f"  {ts}  {r['gemeente_naam']:20s} / {r['buurt_naam']:25s}  "
                f"{r['dur_min']:.1f} min"
            )
        lines.append("")

    # Recent failures
    if recent_failed:
        lines.append(f"{RED}{BOLD}Recent failures:{RESET}")
        for r in recent_failed:
            lines.append(
                f"  {r['gemeente_naam']:20s} / {r['buurt_naam']:25s}  "
                f"attempt {r['attempt_count']}  "
                f"{(r['err'] or '').splitlines()[0][:50]}"
            )

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Isochrone pipeline monitor")
    parser.add_argument("--dsn", required=True, help="PostgreSQL DSN")
    parser.add_argument(
        "--refresh", type=int, default=15,
        help="Refresh interval in seconds (default: 15)",
    )
    parser.add_argument(
        "--auto-reset", action="store_true",
        help="Automatically reset stale jobs (> 4h) on each refresh",
    )
    args = parser.parse_args()

    conn = psycopg2.connect(args.dsn)

    try:
        while True:
            if args.auto_reset:
                with conn.cursor() as cur:
                    cur.execute("SELECT reset_stale_jobs()")
                    n = cur.fetchone()[0]
                    conn.commit()
                    if n:
                        print(f"Auto-reset {n} stale jobs.")

            stats = fetch_stats(conn)
            counts = stats[0]
            output = render(*stats, refresh=args.refresh)

            sys.stdout.write(CLEAR)
            sys.stdout.write(output + "\n")
            sys.stdout.flush()

            total = sum(counts.values())
            done   = counts.get("done", 0)
            failed = counts.get("failed", 0)
            if done + failed == total and total > 0:
                print("\nAll jobs finished.")
                break

            time.sleep(args.refresh)

    except KeyboardInterrupt:
        print("\nMonitor stopped.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
