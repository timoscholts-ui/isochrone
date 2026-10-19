//! Shared library for the isochrone backend: graph, dijkstra, geometry, KPI,
//! CRS transform, and data loading. Ported from `Downloads/railway-deploy/`.

pub mod crs;
pub mod dijkstra;
pub mod geojson;
pub mod geometry;
pub mod graph;
pub mod kpi;

pub use geo;

/// Round `x` to `ndigits` decimal places (Python `round`-like).
#[must_use]
pub fn round_dec(x: f64, ndigits: i32) -> f64 {
    let f = 10_f64.powi(ndigits);
    (x * f).round() / f
}
