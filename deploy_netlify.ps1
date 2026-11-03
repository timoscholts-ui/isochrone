# deploy_netlify.ps1
# Bundles index.html + static heatmap data into a deploy/ folder and pushes to Netlify.
#
# Usage (from isochrone-rust/ directory):
#   .\deploy_netlify.ps1
#   .\deploy_netlify.ps1 -DryRun   # prepare folder only, don't deploy

param([switch]$DryRun)

$ErrorActionPreference = 'Stop'

$SCRIPT_DIR  = Split-Path -Parent $MyInvocation.MyCommand.Path
$VIEWER_SRC  = "C:\Users\Timo\Downloads\isochrone_viewer"
$DEPLOY_DIR  = "$SCRIPT_DIR\netlify-deploy"

# Completed areas to include in the showcase (slug, display name)
$AREAS = @(
    @{ slug = "bunnik";              name = "Bunnik" },
    @{ slug = "houten";              name = "Houten" },
    @{ slug = "rhenen";              name = "Rhenen" },
    @{ slug = "utrechtse_heuvelrug"; name = "Utrechtse Heuvelrug" },
    @{ slug = "wijk_oost";           name = "Wijk bij Duurstede Oost" },
    @{ slug = "zeist";               name = "Zeist" }
)

# ── 1. Prepare deploy directory ──────────────────────────────────────────────
Write-Host "Preparing $DEPLOY_DIR ..."
if (Test-Path $DEPLOY_DIR) { Remove-Item $DEPLOY_DIR -Recurse -Force }
New-Item -ItemType Directory -Path "$DEPLOY_DIR\isochrone_viewer\heatmaps" -Force | Out-Null

# ── 2. Copy index.html ────────────────────────────────────────────────────────
Copy-Item "$SCRIPT_DIR\index.html" "$DEPLOY_DIR\index.html"
Write-Host "  index.html"

# ── 3. Heatmap assets (manifest, region boundary, 26 PNGs) ───────────────────
Copy-Item "$VIEWER_SRC\heatmap_manifest.json"  "$DEPLOY_DIR\isochrone_viewer\"
Copy-Item "$VIEWER_SRC\heatmap_region.geojson" "$DEPLOY_DIR\isochrone_viewer\"
Copy-Item "$VIEWER_SRC\heatmaps\*"             "$DEPLOY_DIR\isochrone_viewer\heatmaps\"
$pngCount = (Get-ChildItem "$DEPLOY_DIR\isochrone_viewer\heatmaps\*.png").Count
Write-Host "  heatmap_manifest.json, heatmap_region.geojson, $pngCount PNGs"

# ── 4. Per-area static files (supermarkten + buurten for isochrone tab) ───────
foreach ($a in $AREAS) {
    $slug = $a.slug
    foreach ($file in @("${slug}_supermarkten.json", "${slug}_buurten_wgs84.geojson")) {
        $src = "$VIEWER_SRC\$file"
        if (Test-Path $src) {
            Copy-Item $src "$DEPLOY_DIR\isochrone_viewer\"
        } else {
            Write-Warning "  Missing: $file (isochrone tab won't show area overlay)"
        }
    }
}
Write-Host "  per-area supermarkten + buurten"

# ── 5. Write fresh areas.json with only the completed areas ──────────────────
$areasJson = $AREAS | ConvertTo-Json -Depth 2
Set-Content "$DEPLOY_DIR\isochrone_viewer\areas.json" $areasJson -Encoding UTF8
Write-Host "  areas.json ($($AREAS.Count) areas)"

# ── 6. Summary ────────────────────────────────────────────────────────────────
$total = (Get-ChildItem $DEPLOY_DIR -Recurse -File).Count
$sizeMB = [math]::Round((Get-ChildItem $DEPLOY_DIR -Recurse -File | Measure-Object -Property Length -Sum).Sum / 1MB, 1)
Write-Host ""
Write-Host "Deploy folder ready: $total files, $sizeMB MB"
Write-Host ""

if ($DryRun) {
    Write-Host "-DryRun: skipping netlify deploy."
    Write-Host "Run without -DryRun to publish."
    exit 0
}

# ── 7. Deploy ─────────────────────────────────────────────────────────────────
Write-Host "Deploying to Netlify ..."
netlify deploy --prod --dir $DEPLOY_DIR
