# Worker Laptop Setup — NL Isochrone Pipeline

Each worker laptop runs `nl_worker.py`, which claims buurt jobs from a shared
Postgres database via an SSH tunnel. This document covers everything a laptop
needs to participate.

---

## 1. Input data files

Copy these to the same paths on every worker (or override with env vars — see §4).

| File | Size | Path on laptop |
|------|------|----------------|
| `netherlands-latest.osm.pbf` | 1.4 GB | `C:\Users\<user>\Downloads\netherlands-latest.osm.pbf` |
| `bag-export.sqlite` | 8.9 GB | `C:\Users\<user>\Downloads\pcodes-sqlite\bag-export.sqlite` |
| `Supermarkt_lidl (2).gpkg` | 3.1 MB | `C:\Users\<user>\Downloads\Supermarkt_lidl (2).gpkg` |

**CBS buurten** are fetched automatically from the AGOL FeatureServer at runtime
(requires internet + AGOL credentials in §4). No local file needed.

### Transfer options

```powershell
# USB / external drive — fastest for the large files
robocopy D:\isochrone_data C:\Users\<user>\Downloads netherlands-latest.osm.pbf
robocopy D:\isochrone_data\pcodes-sqlite C:\Users\<user>\Downloads\pcodes-sqlite bag-export.sqlite
robocopy D:\isochrone_data "C:\Users\<user>\Downloads" "Supermarkt_lidl (2).gpkg"

# rsync over LAN (from main laptop, once SSH is set up)
rsync -avz --progress timo@<main-laptop-ip>:Downloads/netherlands-latest.osm.pbf Downloads/
rsync -avz --progress timo@<main-laptop-ip>:Downloads/pcodes-sqlite/ Downloads/pcodes-sqlite/
rsync -avz --progress "timo@<main-laptop-ip>:Downloads/Supermarkt_lidl (2).gpkg" Downloads/
```

---

## 2. Python scripts

Copy the entire `isochrone-rust/` folder, or at minimum these files:

```
nl_worker.py              # main worker entry point
nl_areas.py               # monkey-patch for isochrone_areas
nl_bag.py                 # BAG address loading (replaces build_bag_population)
build_focus_isochrones.py # OSM network + isochrone computation
build_kpi_lookup.py       # WVO / population KPI calculation
build_viewer_data.py      # WGS84 reprojection for viewer output
isochrone_areas.py        # original (must exist even though nl_areas overrides it)
```

> `build_bag_population.py`, `build_heatmaps.py`, `server.py` are **not** needed on workers.

---

## 3. Python environment

Python 3.12+ recommended. Install dependencies:

```powershell
pip install networkx==3.6.1 osmnx==2.1.1 pyrosm==0.20.0 shapely==2.1.2 ^
            pyproj==3.7.2 numpy==2.3.4 pandas==2.3.3 geopandas==1.1.2 ^
            scipy==1.17.1 psycopg2-binary
```

Or use the repo's `requirements.txt` + add `psycopg2-binary`:

```powershell
pip install -r requirements.txt psycopg2-binary
```

---

## 4. Environment variables

Set these before starting the worker:

```powershell
# AGOL credentials (for CBS buurten FeatureServer)
$env:AGOL_USERNAME = "timo_PerfectPlace"
$env:AGOL_PASSWORD = "Espresso25!!"

# Data paths — only needed if files are NOT at the default locations above
$env:BAG_SQLITE    = "C:\Users\<user>\Downloads\pcodes-sqlite\bag-export.sqlite"
$env:LOCATUS_GPKG  = "C:\Users\<user>\Downloads\Supermarkt_lidl (2).gpkg"
$env:ISO_PBF       = "C:\Users\<user>\Downloads\netherlands-latest.osm.pbf"

# Optional: keep temp output files after each job (for debugging)
# $env:ISO_KEEP_FILES = "1"
```

---

## 5. SSH tunnel + database connection

> **⚠ NOT YET SET UP — see `ssh_tunnel_TODO.md`**
>
> The Postgres database lives on the main laptop (Timo's machine). Workers
> connect via an SSH tunnel. Fill in `ssh_tunnel_TODO.md` once the server is
> configured, then distribute it to all workers.

Once the tunnel is running, start the worker:

```powershell
/c/Python314/python.exe -X utf8 nl_worker.py `
    --dsn postgresql://iso_worker:changeme@localhost:15432/isochrone_db `
    --max-ram 8 `
    --worker-id laptop-arjan
```

Adjust `--max-ram` to the laptop's available RAM (leave ~2 GB headroom for OS).

### Single test job (no tunnel needed on main laptop)

```powershell
/c/Python314/python.exe -X utf8 nl_worker.py `
    --dsn postgresql://iso_worker:changeme@localhost:5432/isochrone_db `
    --max-ram 20 --worker-id test --once
```

---

## 6. Checklist before starting

- [ ] `netherlands-latest.osm.pbf` present (1.4 GB)
- [ ] `bag-export.sqlite` present (8.9 GB)
- [ ] `Supermarkt_lidl (2).gpkg` present (3.1 MB)
- [ ] Python packages installed (`import psycopg2, geopandas, osmnx, pyrosm` all work)
- [ ] AGOL credentials set in env
- [ ] SSH tunnel running (see `ssh_tunnel_TODO.md`)
- [ ] `nl_worker.py` starts without import errors
