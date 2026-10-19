//! KPI computation over an isochrone polygon (ported from `_compute_kpi`).
//!
//! Supermarkets and BAG points use `contains`; buurten use `intersects` with
//! area-proportional population weighting.

use std::collections::BTreeMap;

use geo::{Area, BooleanOps, Contains, MultiPolygon, Point};
use rstar::primitives::GeomWithData;
use rstar::{RTree, RTreeObject, AABB};

use crate::round_dec;

/// Minimum population for a meaningful WVO-per-inhabitant ratio.
pub const POP_FLOOR: f64 = 50.0;

/// A supermarket point (RD) with its WVO floor area (m²).
pub struct Supermarkt {
    pub x: f64,
    pub y: f64,
    pub wvo: f64,
}

/// A BAG address point (RD) with its population share.
pub struct BagPoint {
    pub x: f64,
    pub y: f64,
    pub pop: f64,
}

/// A buurt (neighbourhood) polygon (RD) with its attributes.
pub struct Buurt {
    pub code: String,
    pub naam: String,
    pub inwoners: f64,
    pub area_slug: String,
    pub geom: MultiPolygon<f64>,
}

/// Computed KPI result (rounded like the Python reference).
#[derive(Debug, Clone, serde::Serialize)]
pub struct Kpi {
    pub wvo: f64,
    pub pop_bag: f64,
    pub pop_area: f64,
    pub wvo_per_inwoner_bag: Option<f64>,
    pub wvo_per_inwoner_area: Option<f64>,
    pub buurt_dekking: BTreeMap<String, f64>,
}

/// rstar object wrapping a buurt's bounding box + index into the buurt vec.
struct BuurtGeom {
    bbox: AABB<[f64; 2]>,
    idx: usize,
}

impl RTreeObject for BuurtGeom {
    type Envelope = AABB<[f64; 2]>;

    fn envelope(&self) -> Self::Envelope {
        self.bbox
    }
}

/// Holds the rstar indexes + attribute data used by `compute`.
pub struct KpiEngine {
    sm_tree: RTree<GeomWithData<[f64; 2], f64>>,
    bag_tree: RTree<GeomWithData<[f64; 2], f64>>,
    buurten: Vec<Buurt>,
    buurt_areas: Vec<f64>,
    buurt_tree: RTree<BuurtGeom>,
}

impl KpiEngine {
    #[must_use]
    pub fn new(supermarkten: Vec<Supermarkt>, bag: Vec<BagPoint>, buurten: Vec<Buurt>) -> Self {
        let sm_tree = RTree::bulk_load(
            supermarkten
                .iter()
                .map(|s| GeomWithData::new([s.x, s.y], s.wvo))
                .collect(),
        );
        let bag_tree = RTree::bulk_load(
            bag.iter()
                .map(|b| GeomWithData::new([b.x, b.y], b.pop))
                .collect(),
        );
        let buurt_areas: Vec<f64> = buurten.iter().map(|b| b.geom.unsigned_area()).collect();
        let buurt_tree = RTree::bulk_load(
            buurten
                .iter()
                .enumerate()
                .map(|(i, b)| BuurtGeom {
                    bbox: poly_aabb(&b.geom),
                    idx: i,
                })
                .collect(),
        );
        Self {
            sm_tree,
            bag_tree,
            buurten,
            buurt_areas,
            buurt_tree,
        }
    }

    #[must_use]
    pub fn compute(&self, poly: &MultiPolygon<f64>) -> Kpi {
        let aabb = poly_aabb(poly);

        let mut wvo = 0.0;
        for g in self.sm_tree.locate_in_envelope(aabb) {
            let [x, y] = *g.geom();
            if poly.contains(&Point::new(x, y)) {
                wvo += g.data;
            }
        }

        let mut pop_bag = 0.0;
        for g in self.bag_tree.locate_in_envelope(aabb) {
            let [x, y] = *g.geom();
            if poly.contains(&Point::new(x, y)) {
                pop_bag += g.data;
            }
        }

        let mut dek = BTreeMap::new();
        let mut pop_area = 0.0;
        for g in self.buurt_tree.locate_in_envelope(aabb) {
            let b = &self.buurten[g.idx];
            let inter = poly.intersection(&b.geom).unsigned_area();
            if inter > 0.0 {
                let frac = inter / self.buurt_areas[g.idx];
                dek.insert(b.code.clone(), round_dec(frac, 4));
                pop_area += b.inwoners * frac;
            }
        }

        Kpi {
            wvo: round_dec(wvo, 1),
            pop_bag: round_dec(pop_bag, 1),
            pop_area: round_dec(pop_area, 1),
            wvo_per_inwoner_bag: (pop_bag >= POP_FLOOR)
                .then(|| round_dec(wvo / pop_bag, 4)),
            wvo_per_inwoner_area: (pop_area >= POP_FLOOR)
                .then(|| round_dec(wvo / pop_area, 4)),
            buurt_dekking: dek,
        }
    }
}

fn poly_aabb(poly: &MultiPolygon<f64>) -> AABB<[f64; 2]> {
    let mut minx = f64::INFINITY;
    let mut miny = f64::INFINITY;
    let mut maxx = f64::NEG_INFINITY;
    let mut maxy = f64::NEG_INFINITY;
    for p in &poly.0 {
        for c in &p.exterior().0 {
            minx = minx.min(c.x);
            miny = miny.min(c.y);
            maxx = maxx.max(c.x);
            maxy = maxy.max(c.y);
        }
    }
    AABB::from_corners([minx, miny], [maxx, maxy])
}
