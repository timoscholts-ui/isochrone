//! On-the-fly isochrone HTTP server (axum). Ported from `server.py`.
//!
//! Loads the precomputed drive/walk graphs + buurten (from `OUT_DIR`) and the
//! supermarket/BAG points (from `VIEWER_DIR`) once at startup, then answers
//! `GET /isochrone?lat=&lon=&mode=&minuten=`.

use std::collections::HashSet;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use axum::extract::{Query, State};
use axum::http::StatusCode;
use axum::routing::get;
use axum::{Json, Router};
use clap::Parser;
use serde::Deserialize;
use serde_json::{json, Value};
use tower_http::cors::CorsLayer;
use tower_http::services::ServeDir;
use tracing::info;
use tracing_subscriber::EnvFilter;

use isochrone_core::dijkstra::DijkstraCache;
use isochrone_core::geojson::{geom_to_geojson, multipolygon_rd_to_wgs84};
use isochrone_core::geometry::poly_for_threshold;
use isochrone_core::graph::Graph;
use isochrone_core::kpi::{BagPoint, Buurt, Kpi, KpiEngine, Supermarkt};
use isochrone_core::{crs, round_dec};

const BIKE_SPEED_RATIO: f64 = 3.0;
const DIJKSTRA_CACHE_MAX: usize = 64;

#[derive(Parser, Debug)]
#[command(name = "isochrone-server", version, about)]
struct Args {
    /// Directory holding drive.graph.bin / walk.graph.bin / buurten.json.
    #[arg(long, env = "OUT_DIR", default_value = "data")]
    out_dir: PathBuf,

    /// Directory holding {slug}_supermarkten.json and {slug}_bag_adressen.json.
    #[arg(long, env = "VIEWER_DIR", default_value = "isochrone_viewer")]
    viewer_dir: PathBuf,

    /// Optional directory of static files to serve at `/`.
    #[arg(long, env = "STATIC_DIR")]
    static_dir: Option<PathBuf>,

    #[arg(long, env = "PORT", default_value = "8000")]
    port: u16,

    #[arg(long, env = "HOST", default_value = "0.0.0.0")]
    host: String,
}

struct AppState {
    drive: Graph,
    walk: Graph,
    kpi: KpiEngine,
    cache: DijkstraCache,
}

#[derive(Deserialize)]
struct SupermarktJson {
    naam: String,
    #[serde(default)]
    adres: String,
    #[serde(default)]
    wvo: Option<f64>,
    lon: f64,
    lat: f64,
}

#[derive(Deserialize)]
struct BagJson {
    x: f64,
    y: f64,
    #[serde(default)]
    pop: f64,
}

#[derive(Deserialize)]
struct BuurtJson {
    buurtcode: String,
    #[serde(default)]
    buurtnaam: String,
    #[serde(default)]
    aantal_inwoners: f64,
    #[serde(default)]
    area: String,
    geometry: Value,
}

// ---------------------------------------------------------------------------
// Startup data
// ---------------------------------------------------------------------------
fn load_supermarkten(viewer_dir: &Path, slugs: &[String]) -> Vec<Supermarkt> {
    let mut seen = HashSet::new();
    let mut out = Vec::new();
    for slug in slugs {
        let path = viewer_dir.join(format!("{slug}_supermarkten.json"));
        let data: Vec<SupermarktJson> = read_json(&path).unwrap_or_default();
        for s in data {
            if !seen.insert(format!("{}|{}", s.naam, s.adres)) {
                continue;
            }
            let (x, y) = crs::to_rd(s.lon, s.lat);
            out.push(Supermarkt {
                x,
                y,
                wvo: s.wvo.unwrap_or(0.0),
            });
        }
    }
    out
}

fn load_bag(viewer_dir: &Path, slugs: &[String]) -> Vec<BagPoint> {
    let mut seen = HashSet::new();
    let mut out = Vec::new();
    for slug in slugs {
        let path = viewer_dir.join(format!("{slug}_bag_adressen.json"));
        let data: Vec<BagJson> = read_json(&path).unwrap_or_default();
        for b in data {
            if !seen.insert((b.x.to_bits(), b.y.to_bits())) {
                continue;
            }
            out.push(BagPoint {
                x: b.x,
                y: b.y,
                pop: b.pop,
            });
        }
    }
    out
}

fn load_buurten(out_dir: &Path) -> anyhow::Result<Vec<Buurt>> {
    let data: Vec<BuurtJson> = read_json(&out_dir.join("buurten.json"))?;
    Ok(data
        .into_iter()
        .map(|b| Buurt {
            code: b.buurtcode,
            naam: b.buurtnaam,
            inwoners: b.aantal_inwoners.max(0.0),
            area_slug: b.area,
            geom: geojson_value_to_multipolygon(&b.geometry),
        })
        .collect())
}

fn read_json<T: serde::de::DeserializeOwned>(path: &Path) -> anyhow::Result<Vec<T>> {
    let bytes = match std::fs::read(path) {
        Ok(b) => b,
        Err(_) => return Ok(Vec::new()),
    };
    Ok(serde_json::from_slice(&bytes)?)
}

/// Convert a GeoJSON `{type, coordinates}` value to a `MultiPolygon`.
fn geojson_value_to_multipolygon(v: &Value) -> isochrone_core::geo::MultiPolygon<f64> {
    use isochrone_core::geo::MultiPolygon;
    let empty = MultiPolygon::new(Vec::new());
    let typ = v.get("type").and_then(|t| t.as_str()).unwrap_or("");
    let coords = match v.get("coordinates") {
        Some(c) => c,
        None => return empty,
    };
    match typ {
        "Polygon" => MultiPolygon::new(vec![polygon_from_rings(coords)]),
        "MultiPolygon" => coords
            .as_array()
            .map(|polys| MultiPolygon::new(polys.iter().map(polygon_from_rings).collect()))
            .unwrap_or(empty),
        _ => empty,
    }
}

fn polygon_from_rings(rings: &Value) -> isochrone_core::geo::Polygon<f64> {
    use isochrone_core::geo::{LineString, Polygon};
    let mut iter = rings.as_array().cloned().unwrap_or_default().into_iter();
    let exterior = iter
        .next()
        .map(ring_to_linestring)
        .unwrap_or_else(|| LineString::new(Vec::new()));
    let interiors = iter.map(ring_to_linestring).collect();
    Polygon::new(exterior, interiors)
}

fn ring_to_linestring(ring: Value) -> isochrone_core::geo::LineString<f64> {
    use isochrone_core::geo::{Coord, LineString};
    let coords = ring
        .as_array()
        .map(|pts| {
            pts.iter()
                .filter_map(|p| {
                    let a = p.as_array()?;
                    Some(Coord {
                        x: a.first()?.as_f64()?,
                        y: a.get(1)?.as_f64()?,
                    })
                })
                .collect()
        })
        .unwrap_or_default();
    LineString::new(coords)
}

// ---------------------------------------------------------------------------
// Handler
// ---------------------------------------------------------------------------
#[derive(Deserialize)]
struct IsoQuery {
    lat: f64,
    lon: f64,
    #[serde(default = "default_mode")]
    mode: String,
    #[serde(default = "default_minuten")]
    minuten: i32,
}

fn default_mode() -> String {
    "auto".to_string()
}

fn default_minuten() -> i32 {
    10
}

async fn isochrone(
    State(state): State<Arc<AppState>>,
    Query(q): Query<IsoQuery>,
) -> Result<Json<Value>, (StatusCode, String)> {
    if q.mode != "auto" && q.mode != "fiets" && q.mode != "lopen" {
        return Err((
            StatusCode::BAD_REQUEST,
            "mode moet auto/fiets/lopen zijn".to_string(),
        ));
    }
    let minuten = q.minuten.clamp(1, 20) as f64;
    let (x, y) = crs::to_rd(q.lon, q.lat);
    let drive_node = state.drive.nearest_node(x, y);
    let walk_node = state.walk.nearest_node(x, y);

    let state = Arc::clone(&state);
    let mode = q.mode.clone();
    let result = tokio::task::spawn_blocking(move || {
        compute_isochrone(&state, &mode, minuten, x, y, drive_node, walk_node)
    })
    .await
    .map_err(|e| (StatusCode::INTERNAL_SERVER_ERROR, e.to_string()))?;

    Ok(Json(result))
}

fn compute_isochrone(
    state: &AppState,
    mode: &str,
    minuten: f64,
    x: f64,
    y: f64,
    drive_node: u32,
    walk_node: u32,
) -> Value {
    let (graph, src, t, name) = match mode {
        "auto" => (&state.drive, drive_node, minuten, "drive"),
        "lopen" => (&state.walk, walk_node, minuten, "walk"),
        _ => (&state.walk, walk_node, minuten * BIKE_SPEED_RATIO, "walk"),
    };

    let dist = state.cache.get_or_compute(graph, name, src);
    let poly = poly_for_threshold(graph, &dist, t);

    let geometry = poly.as_ref().and_then(|p| {
        let wgs = multipolygon_rd_to_wgs84(p);
        geom_to_geojson(&wgs, 5)
    });

    let kpi = poly.as_ref().map_or_else(
        || empty_kpi(),
        |p| state.kpi.compute(p),
    );

    json!({
        "geometry": geometry,
        "kpi": kpi,
        "focus": {"x": round_dec(x, 1), "y": round_dec(y, 1)},
    })
}

fn empty_kpi() -> Kpi {
    Kpi {
        wvo: 0.0,
        pop_bag: 0.0,
        pop_area: 0.0,
        wvo_per_inwoner_bag: None,
        wvo_per_inwoner_area: None,
        buurt_dekking: Default::default(),
    }
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------
#[tokio::main]
async fn main() -> anyhow::Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(
            EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new("info")),
        )
        .init();

    let args = Args::parse();

    let t0 = std::time::Instant::now();
    info!("[startup] netwerk laden ({})…", args.out_dir.display());
    let drive = Graph::load(&args.out_dir.join("drive.graph.bin"))?;
    let walk = Graph::load(&args.out_dir.join("walk.graph.bin"))?;
    info!(
        "[startup] netwerk klaar in {:.1}s (drive {}n/{}e, walk {}n/{}e)",
        t0.elapsed().as_secs_f64(),
        drive.node_count(),
        drive.edges_geom.len(),
        walk.node_count(),
        walk.edges_geom.len()
    );

    info!("[startup] buurten laden…");
    let buurten = load_buurten(&args.out_dir)?;
    let mut slugs: Vec<String> = buurten
        .iter()
        .map(|b| b.area_slug.clone())
        .collect::<HashSet<_>>()
        .into_iter()
        .collect();
    slugs.sort();

    info!("[startup] supermarkten laden…");
    let supermarkten = load_supermarkten(&args.viewer_dir, &slugs);
    info!("[startup] supermarkten: {}", supermarkten.len());

    info!("[startup] BAG-adrespunten laden…");
    let bag = load_bag(&args.viewer_dir, &slugs);
    info!("[startup] BAG-punten: {}", bag.len());

    info!("[startup] KPI-index bouwen…");
    let kpi = KpiEngine::new(supermarkten, bag, buurten);
    info!("[startup] klaar in {:.1}s", t0.elapsed().as_secs_f64());

    let state = Arc::new(AppState {
        drive,
        walk,
        kpi,
        cache: DijkstraCache::new(DIJKSTRA_CACHE_MAX),
    });

    let mut app = Router::new()
        .route("/isochrone", get(isochrone))
        .layer(CorsLayer::permissive())
        .with_state(state);

    if let Some(static_dir) = args.static_dir {
        app = app.fallback_service(ServeDir::new(static_dir));
    }

    let addr = format!("{}:{}", args.host, args.port);
    let listener = tokio::net::TcpListener::bind(&addr).await?;
    info!("luistert op http://{addr}");
    axum::serve(listener, app).await?;
    Ok(())
}
