//! Isochrone polygon construction: reachable-edge buffer → union → simplify →
//! small-hole fill. Ported from `build_focus_isochrones.py`.

use geo::algorithm::buffer::{BufferStyle, LineCap, LineJoin};
use geo::{
    Area, Buffer, LineString, MultiLineString, MultiPolygon, Polygon, Simplify, unary_union,
};

use crate::graph::Graph;

/// Match GEOS `quad_segs=2` (8 segments per full circle) for parity + speed.
const BUFFER_ANGLE: f64 = 0.785_398; // 45° in radians

/// Buffer width (m) around the reachable network (matches Python `EDGE_BUFF`).
pub const EDGE_BUFF: f64 = 300.0;
/// Douglas-Peucker simplify tolerance (m) — matches Python `SIMPLIFY_TOL`.
pub const SIMPLIFY_TOL: f64 = 30.0;
/// Interior rings ≤ this area (m²) are dropped — matches `MAX_HOLE_AREA`.
pub const MAX_HOLE_AREA: f64 = 5000.0;

/// Buffer line count per chunk before incremental union (matches Python).
const CHUNK: usize = 20_000;

/// Union of buffered reachable edges (the "network buffer", not a convex hull).
///
/// `node_filter` (optional) marks reachable nodes; only edges whose *both*
/// endpoints are reachable are buffered.
pub fn buffer_union(
    graph: &Graph,
    edge_buff: f64,
    node_filter: Option<&[bool]>,
) -> Option<MultiPolygon<f64>> {
    let mut lines: Vec<LineString<f64>> = Vec::new();
    for &(u, v) in &graph.edges_geom {
        if let Some(filter) = node_filter {
            if !filter[u as usize] || !filter[v as usize] {
                continue;
            }
        }
        lines.push(LineString::from(vec![
            (graph.node_x[u as usize], graph.node_y[u as usize]),
            (graph.node_x[v as usize], graph.node_y[v as usize]),
        ]));
    }
    if lines.is_empty() {
        return None;
    }

    let style = BufferStyle::new(edge_buff)
        .line_cap(LineCap::Round(BUFFER_ANGLE))
        .line_join(LineJoin::Round(BUFFER_ANGLE));
    let mut parts: Vec<MultiPolygon<f64>> = Vec::new();
    for chunk in lines.chunks(CHUNK) {
        let mls = MultiLineString::new(chunk.to_vec());
        parts.push(mls.buffer_with_style(style.clone()));
    }
    if parts.len() == 1 {
        return parts.pop();
    }
    Some(unary_union(parts.iter()))
}

/// Simplify with tolerance, guarding against a degenerate empty result.
#[must_use]
pub fn simplify(poly: &MultiPolygon<f64>) -> MultiPolygon<f64> {
    let s = poly.simplify(SIMPLIFY_TOL);
    if s.0.is_empty() {
        poly.clone()
    } else {
        s
    }
}

/// Drop interior rings ≤ `max_area` m², per polygon.
#[must_use]
pub fn fill_holes(poly: &MultiPolygon<f64>, max_area: f64) -> MultiPolygon<f64> {
    MultiPolygon::new(poly.0.iter().map(|p| fill_holes_polygon(p, max_area)).collect())
}

fn fill_holes_polygon(poly: &Polygon<f64>, max_area: f64) -> Polygon<f64> {
    let exterior = poly.exterior().clone();
    let holes: Vec<LineString<f64>> = poly
        .interiors()
        .iter()
        .filter(|h| Polygon::new((*h).clone(), Vec::new()).unsigned_area() > max_area)
        .cloned()
        .collect();
    Polygon::new(exterior, holes)
}

/// Full polygon for a travel-time threshold: reachable nodes → buffered union →
/// simplify → fill holes.
#[must_use]
pub fn poly_for_threshold(graph: &Graph, dist: &[f64], t: f64) -> Option<MultiPolygon<f64>> {
    let mut reachable = vec![false; dist.len()];
    let mut any = false;
    for (i, &d) in dist.iter().enumerate() {
        if d <= t {
            reachable[i] = true;
            any = true;
        }
    }
    if !any {
        return None;
    }
    let poly = buffer_union(graph, EDGE_BUFF, Some(&reachable))?;
    if poly.0.is_empty() {
        return None;
    }
    Some(fill_holes(&simplify(&poly), MAX_HOLE_AREA))
}
