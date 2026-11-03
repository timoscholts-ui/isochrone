# SSH Tunnel — Handover File

> **Fill in this file once the main laptop's SSH server and Postgres are configured,
> then send it to each worker laptop operator.**

---

## Main laptop (one-time setup)

### 1. Install PostgreSQL 17

```powershell
winget install --id PostgreSQL.PostgreSQL.17 --source winget
```

### 2. Create database and user

```powershell
psql -U postgres
```
```sql
CREATE DATABASE isochrone_db;
CREATE USER iso_worker WITH PASSWORD 'changeme';   -- change this
GRANT ALL PRIVILEGES ON DATABASE isochrone_db TO iso_worker;
\c isochrone_db
-- Run db_migrate.sql:
\i C:/Users/Timo/Downloads/isochrone-rust/db_migrate.sql
```

### 3. Seed jobs

```powershell
$env:AGOL_USERNAME = "timo_PerfectPlace"
$env:AGOL_PASSWORD = "Espresso25!!"
/c/Python314/python.exe -X utf8 C:/Users/Timo/Downloads/isochrone-rust/seed_jobs.py `
    --dsn postgresql://iso_worker:changeme@localhost:5432/isochrone_db
```

### 4. Enable OpenSSH Server

```powershell
Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0
Start-Service sshd
Set-Service sshd -StartupType Automatic
New-NetFirewallRule -Name sshd -DisplayName "OpenSSH Server" `
    -Enabled True -Direction Inbound -Protocol TCP -LocalPort 22 -Action Allow
```

### 5. Find local IP address

```powershell
ipconfig | findstr "IPv4"
```

---

## Connection details  ← FILL IN BEFORE DISTRIBUTING

```
Main laptop IP:    ___.___.___.___
SSH username:      _______________
SSH port:          22  (or _______)
Postgres port:     5432
DB name:           isochrone_db
DB user:           iso_worker
DB password:       _______________   (the password set in step 2)
```

---

## Worker laptop — start tunnel

Open a terminal and run (replace values from the table above):

```powershell
ssh -N -L 15432:localhost:5432 <ssh-username>@<main-laptop-ip>
```

Keep this terminal open while the worker runs. The tunnel forwards
`localhost:15432` on the worker to `localhost:5432` on the main laptop.

### Worker DSN

```
postgresql://iso_worker:<db-password>@localhost:15432/isochrone_db
```

### Full worker start command

```powershell
# Set in a separate terminal before starting the tunnel + worker
$env:AGOL_USERNAME = "timo_PerfectPlace"
$env:AGOL_PASSWORD = "Espresso25!!"

# Then start worker (adjust --max-ram to available RAM)
/c/Python314/python.exe -X utf8 C:\<path-to-scripts>\nl_worker.py `
    --dsn "postgresql://iso_worker:<db-password>@localhost:15432/isochrone_db" `
    --max-ram 8 `
    --worker-id <laptop-name>
```

---

## Monitor (run on main laptop, no tunnel needed)

```powershell
/c/Python314/python.exe -X utf8 C:/Users/Timo/Downloads/isochrone-rust/monitor.py `
    --dsn postgresql://iso_worker:<db-password>@localhost:5432/isochrone_db `
    --refresh 15
```
