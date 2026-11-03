-- NL-wide isochrone pipeline schema (buurt-level jobs)
-- Run once on the Postgres instance that coordinates all workers.
-- Each job = one CBS buurt (~14,913 jobs total).

CREATE TABLE IF NOT EXISTS isochrone_jobs (
    id               SERIAL PRIMARY KEY,
    buurt_code       TEXT    NOT NULL UNIQUE,  -- BU00140000
    buurt_naam       TEXT    NOT NULL,
    gemeente_code    TEXT    NOT NULL,          -- GM0014  (metadata, not unique key)
    gemeente_naam    TEXT    NOT NULL,
    provincie_naam   TEXT    NOT NULL,
    area_km2         REAL    NOT NULL,
    spacing          INTEGER NOT NULL DEFAULT 750,
    est_ram_gb       REAL    NOT NULL,
    status           TEXT    NOT NULL DEFAULT 'queued'
                             CHECK (status IN ('queued', 'running', 'done', 'failed')),
    worker_id        TEXT,
    started_at       TIMESTAMPTZ,
    finished_at      TIMESTAMPTZ,
    error_msg        TEXT,
    attempt_count    INTEGER NOT NULL DEFAULT 0,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Efficient job claiming: workers pick the largest job that fits in their RAM.
CREATE INDEX IF NOT EXISTS idx_jobs_claim
    ON isochrone_jobs (status, est_ram_gb DESC)
    WHERE status = 'queued';

-- Index for gemeente-level aggregation / filtering
CREATE INDEX IF NOT EXISTS idx_jobs_gemeente
    ON isochrone_jobs (gemeente_code);

CREATE TABLE IF NOT EXISTS isochrone_output (
    buurt_code          TEXT PRIMARY KEY,
    buurt_naam          TEXT NOT NULL,
    gemeente_code       TEXT NOT NULL,
    gemeente_naam       TEXT NOT NULL,
    slug                TEXT NOT NULL,         -- bu00140000
    focus_isochrones    JSONB,
    focus_points        JSONB,
    kpi_lookup          JSONB,
    isochrones_wgs84    JSONB,
    buurten_wgs84       JSONB,
    supermarkten        JSONB,
    drive_margin_used   INTEGER,
    walk_margin_used    INTEGER,
    stored_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_output_gemeente
    ON isochrone_output (gemeente_code);

-- Reset jobs that a worker claimed but never finished (crash/OOM).
-- Call once at worker startup; also safe to call from monitor.
CREATE OR REPLACE FUNCTION reset_stale_jobs() RETURNS INTEGER AS $$
DECLARE
    n INTEGER;
BEGIN
    UPDATE isochrone_jobs
       SET status      = 'queued',
           worker_id   = NULL,
           started_at  = NULL
     WHERE status      = 'running'
       AND started_at  < NOW() - INTERVAL '4 hours';
    GET DIAGNOSTICS n = ROW_COUNT;
    RETURN n;
END;
$$ LANGUAGE plpgsql;

-- Convenience view for monitor.py
CREATE OR REPLACE VIEW isochrone_progress AS
SELECT
    status,
    COUNT(*)                                            AS count,
    ROUND(AVG(est_ram_gb)::numeric, 2)                  AS avg_ram_gb,
    COUNT(DISTINCT gemeente_code)                       AS gemeenten
FROM isochrone_jobs
GROUP BY status;
