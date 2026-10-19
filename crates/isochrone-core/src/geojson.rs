//! GeoJSON output for isochrone polygons (ported from `_geom_to_geojson`).

use geo::{Coord, LineString, MultiPolygon, Polygon};
use serde_json::{json, Value};

use crate::{crs, round_dec};

/// Transform a polygon from RD New to WGS84 (in place of the Python
/// `_rd_to_wgs84` shapely transform).
#[must_use]
pub fn multipolygon_rd_to_wgs84(poly: &MultiPolygon<f64>) -> MultiPolygon<f64> {
    MultiPolygon::new(
        poly.0
            .iter()
            .map(|p| {
                Polygon::new(
                    transform_ring(p.exterior()),
                    p.interiors().iter().map(transform_ring).collect(),
                )
            })
            .collect(),
    )
}

fn transform_ring(ring: &LineString<f64>) -> LineString<f64> {
    LineString::new(
        ring.0
            .iter()
            .map(|c| {
                let (lon, lat) = crs::to_wgs84(c.x, c.y);
                Coord { x: lon, y: lat }
            })
            .collect(),
    )
}

/// Convert a (WGS84) `MultiPolygon` to a GeoJSON geometry, or `None` if empty.
#[must_use]
pub fn geom_to_geojson(poly: &MultiPolygon<f64>, ndigits: i32) -> Option<Value> {
    if poly.0.is_empty() {
        return None;
    }
    if poly.0.len() == 1 {
        Some(json!({
            "type": "Polygon",
            "coordinates": polygon_coords(&poly.0[0], ndigits),
        }))
    } else {
        let coords: Vec<_> = poly
            .0
            .iter()
            .map(|p| polygon_coords(p, ndigits))
            .collect();
        Some(json!({
            "type": "MultiPolygon",
            "coordinates": coords,
        }))
    }
}

fn polygon_coords(p: &Polygon<f64>, ndigits: i32) -> Vec<Vec<Vec<f64>>> {
    let mut rings = vec![ring_coords(p.exterior(), ndigits)];
    for h in p.interiors() {
        rings.push(ring_coords(h, ndigits));
    }
    rings
}

fn ring_coords(ring: &LineString<f64>, ndigits: i32) -> Vec<Vec<f64>> {
    ring.0
        .iter()
        .map(|c| vec![round_dec(c.x, ndigits), round_dec(c.y, ndigits)])
        .collect()
}
