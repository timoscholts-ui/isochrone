#!/usr/bin/env bash
# Overnight pipeline (resumed): wijk_bij_duurstede → de_bilt → utrecht → heatmaps → deploy
set -euo pipefail

PYTHON="/c/Python314/python.exe -X utf8"
export ISO_PBF="C:/Users/Timo/Downloads/netherlands-latest.osm.pbf"
SCRIPT_DIR="C:/Users/Timo/Downloads/isochrone-rust"
OUT_DIR="C:/Users/Timo/Downloads/isochrone_viewer"
DEPLOY_DIR="C:/Users/Timo/Downloads/netlify_deploy/isochrone_viewer"

cd "$SCRIPT_DIR"

log() { echo "[$(date '+%H:%M:%S')] $*"; }

wait_for_file() {
    local f="$1" label="$2"
    log "Wachten op $label..."
    while [ ! -f "$f" ]; do sleep 30; done
    log "$label klaar."
}

# Step 1: Wait for wijk_bij_duurstede (still running)
wait_for_file "$OUT_DIR/wijk_bij_duurstede_focus_isochrones.geojson" "wijk_bij_duurstede focus_isochrones"

log "kpi_lookup: wijk_bij_duurstede..."
$PYTHON build_kpi_lookup.py wijk_bij_duurstede

# Step 2: De Bilt (alone)
log "focus_isochrones: de_bilt..."
$PYTHON build_focus_isochrones.py de_bilt

log "kpi_lookup: de_bilt..."
$PYTHON build_kpi_lookup.py de_bilt

# Step 3: Utrecht (reduced margins to avoid OOM)
log "focus_isochrones: utrecht (drive_margin=5000, walk_margin=2500)..."
export ISO_DRIVE_MARGIN=5000
export ISO_WALK_MARGIN=2500
$PYTHON build_focus_isochrones.py utrecht
unset ISO_DRIVE_MARGIN ISO_WALK_MARGIN

log "kpi_lookup: utrecht..."
$PYTHON build_kpi_lookup.py utrecht

# Step 4: Heatmaps (all 6 areas)
log "build_heatmaps.py..."
$PYTHON build_heatmaps.py

# Step 5: Copy to netlify_deploy
log "Copying to netlify_deploy..."
cp "$OUT_DIR/areas.json"             "$DEPLOY_DIR/"
cp "$OUT_DIR/heatmap_manifest.json"  "$DEPLOY_DIR/"
cp "$OUT_DIR/heatmap_region.geojson" "$DEPLOY_DIR/"
mkdir -p "$DEPLOY_DIR/heatmaps"
cp "$OUT_DIR/heatmaps/"* "$DEPLOY_DIR/heatmaps/"

for slug in bunnik houten rhenen wijk_bij_duurstede de_bilt utrecht; do
    cp "$OUT_DIR/${slug}_buurten_wgs84.geojson"  "$DEPLOY_DIR/"
    cp "$OUT_DIR/${slug}_supermarkten.json"       "$DEPLOY_DIR/"
    cp "$OUT_DIR/${slug}_focus_points_wgs84.json" "$DEPLOY_DIR/"
    cp "$OUT_DIR/${slug}_kpi_lookup.json"         "$DEPLOY_DIR/"
done

# Step 6: Netlify deploy
log "Netlify deploy..."
cd "C:/Users/Timo/Downloads/netlify_deploy"
netlify deploy --prod --dir . --site e86e2ae4-999f-4bed-aeaa-5ed4e04a8927

log "Done!"
