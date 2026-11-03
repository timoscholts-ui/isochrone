# -*- coding: utf-8 -*-
"""
Populate isochrone_jobs with one row per CBS buurt (~14,913 jobs).

Each buurt is a separate job so OSM network loading covers a small area,
keeping RAM usage low. The isochrone grid (750 m spacing) runs within the
buurt boundary, same as before but at buurt granularity.

CBS source: FeatureServer CBS_Buurten_2026 (layer 0, WGS84).
Provincie info joined from gemeenten-alfabetisch-2026.csv.

Usage:
    python -X utf8 seed_jobs.py --dsn postgresql://iso_worker:changeme@localhost:5432/isochrone_db
    python -X utf8 seed_jobs.py --dsn ... --reset
"""

import argparse
import json
import os
import urllib.parse
import urllib.request

import geopandas as gpd
import pandas as pd
import psycopg2

CBS_FS_URL = os.environ.get(
    "CBS_BUURTEN_FS_URL",
    "https://services9.arcgis.com/e2SbRUv3uVfnVzwG/arcgis/rest/services/CBS_Buurten_2026/FeatureServer/0",
)
CSV_PATH = os.environ.get(
    "GEMEENTEN_CSV",
    r"C:\Users\Timo\Downloads\gemeenten-alfabetisch-2026.csv",
)

# Exclude water / buitenland buurten
EXCLUDE_GM_CODES = {"GM0998"}


def get_token(username: str, password: str) -> str:
    data = urllib.parse.urlencode({
        "username": username,
        "password": password,
        "referer": "https://www.arcgis.com",
        "expiration": 120,
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
    return resp["token"]


def fetch_all_buurten(token: str) -> gpd.GeoDataFrame:
    """
    Fetch all CBS buurten with geometry (paginated, WGS84 → RD28992).
    Returns GeoDataFrame with: bu_code, bu_naam, gm_code, gm_naam, AantalInwoners,
    area_m2, geometry (EPSG:28992).
    """
    features = []
    offset = 0
    while True:
        params = urllib.parse.urlencode({
            "where": "1=1",
            "outFields": "bu_code,bu_naam,gm_code,gm_naam,AantalInwoners",
            "returnGeometry": "true",
            "resultOffset": offset,
            "resultRecordCount": 2000,
            "outSR": 4326,
            "f": "geojson",
            "token": token,
        })
        url = f"{CBS_FS_URL}/query?{params}"
        page = json.loads(urllib.request.urlopen(url, timeout=120).read())
        batch = page.get("features", [])
        features.extend(batch)
        print(f"  Fetched {len(features)} buurten ...")
        if len(batch) < 2000:
            break
        offset += 2000

    gdf = gpd.GeoDataFrame.from_features(features, crs=4326).to_crs(28992)
    gdf["AantalInwoners"] = pd.to_numeric(gdf["AantalInwoners"], errors="coerce").fillna(0)
    gdf["area_m2"] = gdf.geometry.area
    return gdf


def estimate_ram(area_km2: float, drive_margin_km: float = 10.0) -> float:
    """
    RAM estimate (GB) for a buurt-level job.

    The drive margin is still 10 km (same as gemeente-level), so the OSM network
    covers a ~(sqrt(area) + 10 km)² patch. For tiny buurten (< 1 km²) the
    network is almost entirely margin — about the same cost as a small gemeente.

    Scale: RAM ∝ (sqrt(area) + margin)²
    Baseline: Bunnik gemeente (43 km², 10 km margin) ≈ 3.3 GB observed.
    """
    import math
    baseline_area_km2 = 43.0
    baseline_margin_km = 10.0
    baseline_ram = 3.3

    effective_buurt = (math.sqrt(area_km2) + drive_margin_km) ** 2
    effective_baseline = (math.sqrt(baseline_area_km2) + baseline_margin_km) ** 2
    est = baseline_ram * (effective_buurt / effective_baseline)
    return max(est, 1.0)   # floor at 1 GB (margin alone costs this even for tiny buurten)


def load_csv(path: str) -> pd.DataFrame:
    df = pd.read_csv(path, sep=";", encoding="utf-8-sig", dtype=str)
    df.columns = df.columns.str.strip()
    df["GemeentecodeGM"] = df["GemeentecodeGM"].str.strip()
    return df[["GemeentecodeGM", "Provincienaam"]]


def seed(dsn: str, reset: bool = False):
    username = os.environ.get("AGOL_USERNAME", "timo_PerfectPlace")
    password = os.environ.get("AGOL_PASSWORD", "Espresso25!!")

    print("Requesting AGOL token ...")
    token = get_token(username, password)

    print("Fetching CBS buurten from FeatureServer ...")
    gdf = fetch_all_buurten(token)

    print("Loading CSV for provincie info ...")
    csv_df = load_csv(CSV_PATH)
    gdf = gdf.merge(csv_df, left_on="gm_code", right_on="GemeentecodeGM", how="left")
    gdf["Provincienaam"] = gdf["Provincienaam"].fillna("Onbekend")

    conn = psycopg2.connect(dsn)
    cur = conn.cursor()

    if reset:
        print("--reset: truncating isochrone_jobs and isochrone_output ...")
        cur.execute("TRUNCATE isochrone_jobs, isochrone_output RESTART IDENTITY CASCADE")
        conn.commit()

    inserted = 0
    skipped = 0

    for _, row in gdf.iterrows():
        buurt_code = str(row["bu_code"]).strip()      # BU00140000
        buurt_naam = str(row["bu_naam"]).strip()
        gemeente_code = str(row["gm_code"]).strip()   # GM0014
        gemeente_naam = str(row["gm_naam"]).strip()
        provincie = str(row["Provincienaam"]).strip()

        if gemeente_code in EXCLUDE_GM_CODES:
            skipped += 1
            continue

        # Exclude water / uninhabited (AantalInwoners == -99997 or very negative)
        inwoners = float(row["AantalInwoners"])
        if inwoners < -10000:
            skipped += 1
            continue

        area_km2 = float(row["area_m2"]) / 1_000_000
        if area_km2 < 0.001:   # < 1000 m² — skip slivers
            skipped += 1
            continue

        est_ram = estimate_ram(area_km2)

        cur.execute(
            """
            INSERT INTO isochrone_jobs
                (buurt_code, buurt_naam, gemeente_code, gemeente_naam,
                 provincie_naam, area_km2, spacing, est_ram_gb)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (buurt_code) DO NOTHING
            """,
            (buurt_code, buurt_naam, gemeente_code, gemeente_naam,
             provincie, area_km2, 750, est_ram),
        )
        if cur.rowcount:
            inserted += 1
        else:
            skipped += 1

    conn.commit()
    cur.close()
    conn.close()

    print(f"Inserted {inserted} buurt jobs, skipped {skipped}.")

    conn2 = psycopg2.connect(dsn)
    cur2 = conn2.cursor()
    cur2.execute(
        "SELECT COUNT(*), COUNT(DISTINCT gemeente_code), MIN(est_ram_gb), MAX(est_ram_gb), AVG(est_ram_gb) FROM isochrone_jobs"
    )
    count, ngem, mn, mx, avg = cur2.fetchone()
    print(f"Total jobs: {count} buurten across {ngem} gemeenten  "
          f"RAM range: {mn:.2f}–{mx:.2f} GB  avg: {avg:.2f} GB")
    cur2.close()
    conn2.close()


def main():
    parser = argparse.ArgumentParser(description="Seed isochrone_jobs (buurt-level)")
    parser.add_argument("--dsn", required=True, help="PostgreSQL DSN")
    parser.add_argument("--reset", action="store_true", help="Truncate and reseed")
    args = parser.parse_args()
    seed(args.dsn, reset=args.reset)


if __name__ == "__main__":
    main()
