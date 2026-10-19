//! Road graph loaded from the precomputed `*.graph.bin` files (ISOG format).
//!
//! Builds a CSR adjacency (contiguous arrays) for cache-friendly Dijkstra and a
//! `rstar` tree for nearest-node lookup. Parallel edges are collapsed to the
//! minimum-time edge per (from, to) for routing; a unique (from, to) list is
//! kept for geometry buffering.

use std::collections::{HashMap, HashSet};
use std::fs::File;
use std::io::{BufReader, Read};
use std::path::Path;

use rstar::primitives::GeomWithData;
use rstar::RTree;

/// ISOG binary format header.
const MAGIC: &[u8; 4] = b"ISOG";
const VERSION: u32 = 1;

#[derive(Debug, thiserror::Error)]
pub enum GraphError {
    #[error("io error: {0}")]
    Io(#[from] std::io::Error),
    #[error("bad magic bytes (not an ISOG graph file)")]
    BadMagic,
    #[error("unsupported graph version {0}")]
    BadVersion(u32),
    #[error("truncated graph file")]
    Truncated,
    #[error("edge references unknown node id {0}")]
    UnknownNode(u64),
}

/// A loaded, CSR-indexed directed graph. Node coordinates are in RD New (EPSG:28992).
pub struct Graph {
    pub node_ids: Vec<u64>,
    pub node_x: Vec<f64>,
    pub node_y: Vec<f64>,
    /// Unique (from_idx, to_idx) pairs for geometry buffering.
    pub edges_geom: Vec<(u32, u32)>,
    csr_offsets: Vec<u32>,
    csr_targets: Vec<u32>,
    csr_times: Vec<f64>,
    tree: RTree<GeomWithData<[f64; 2], u32>>,
}

impl Graph {
    /// Load and index a graph from an ISOG binary file.
    pub fn load(path: &Path) -> Result<Self, GraphError> {
        let mut f = BufReader::new(File::open(path)?);
        let mut buf = Vec::new();
        f.read_to_end(&mut buf)?;

        let mut r = Reader::new(&buf);
        if r.take(4)? != MAGIC {
            return Err(GraphError::BadMagic);
        }
        let version = r.u32()?;
        if version != VERSION {
            return Err(GraphError::BadVersion(version));
        }
        let node_count = r.u64()? as usize;
        let edge_count = r.u64()? as usize;

        let mut node_ids = Vec::with_capacity(node_count);
        let mut node_x = Vec::with_capacity(node_count);
        let mut node_y = Vec::with_capacity(node_count);
        let mut id_to_idx = HashMap::with_capacity(node_count);
        for i in 0..node_count {
            let id = r.u64()?;
            let x = r.f64()?;
            let y = r.f64()?;
            node_ids.push(id);
            node_x.push(x);
            node_y.push(y);
            id_to_idx.insert(id, i as u32);
        }

        // Dedup parallel edges: min time for routing, unique (from, to) for geometry.
        let mut edge_min: HashMap<(u32, u32), f64> = HashMap::with_capacity(edge_count);
        let mut geom_seen: HashSet<(u32, u32)> = HashSet::with_capacity(edge_count);
        let mut edges_geom = Vec::with_capacity(edge_count);
        for _ in 0..edge_count {
            let from_id = r.u64()?;
            let to_id = r.u64()?;
            let _length = r.f64()?;
            let time = r.f64()?;
            let _speed_kph = r.f64()?;
            let f = *id_to_idx.get(&from_id).ok_or(GraphError::UnknownNode(from_id))?;
            let t = *id_to_idx.get(&to_id).ok_or(GraphError::UnknownNode(to_id))?;
            edge_min
                .entry((f, t))
                .and_modify(|e| {
                    if time < *e {
                        *e = time;
                    }
                })
                .or_insert(time);
            if geom_seen.insert((f, t)) {
                edges_geom.push((f, t));
            }
        }

        let (csr_offsets, csr_targets, csr_times) = build_csr(node_count, &edge_min);

        let tree = RTree::bulk_load(
            (0..node_count)
                .map(|i| GeomWithData::new([node_x[i], node_y[i]], i as u32))
                .collect(),
        );

        Ok(Self {
            node_ids,
            node_x,
            node_y,
            edges_geom,
            csr_offsets,
            csr_targets,
            csr_times,
            tree,
        })
    }

    #[must_use]
    pub fn node_count(&self) -> usize {
        self.node_x.len()
    }

    /// Outgoing edges of `node` in CSR form: `(target, time)` pairs.
    #[must_use]
    pub fn outgoing(&self, node: u32) -> impl Iterator<Item = (u32, f64)> + '_ {
        let start = self.csr_offsets[node as usize] as usize;
        let end = self.csr_offsets[node as usize + 1] as usize;
        (start..end).map(move |i| (self.csr_targets[i], self.csr_times[i]))
    }

    /// Index of the node nearest to (x, y) in RD coordinates.
    #[must_use]
    pub fn nearest_node(&self, x: f64, y: f64) -> u32 {
        self.tree.nearest_neighbor([x, y]).map_or(0, |g| g.data)
    }
}

fn build_csr(
    node_count: usize,
    edge_min: &HashMap<(u32, u32), f64>,
) -> (Vec<u32>, Vec<u32>, Vec<f64>) {
    let mut csr_offsets = vec![0u32; node_count + 1];
    for &(f, _) in edge_min.keys() {
        csr_offsets[f as usize + 1] += 1;
    }
    for i in 0..node_count {
        csr_offsets[i + 1] += csr_offsets[i];
    }
    let m = edge_min.len();
    let mut csr_targets = vec![0u32; m];
    let mut csr_times = vec![0.0f64; m];
    let mut cursor = csr_offsets.clone();
    for (&(f, t), &time) in edge_min {
        let idx = cursor[f as usize] as usize;
        csr_targets[idx] = t;
        csr_times[idx] = time;
        cursor[f as usize] += 1;
    }
    (csr_offsets, csr_targets, csr_times)
}

/// Minimal little-endian cursor over a byte slice.
struct Reader<'a> {
    b: &'a [u8],
    pos: usize,
}

impl<'a> Reader<'a> {
    fn new(b: &'a [u8]) -> Self {
        Self { b, pos: 0 }
    }

    fn take(&mut self, n: usize) -> Result<&'a [u8], GraphError> {
        if self.pos + n > self.b.len() {
            return Err(GraphError::Truncated);
        }
        let s = &self.b[self.pos..self.pos + n];
        self.pos += n;
        Ok(s)
    }

    fn u32(&mut self) -> Result<u32, GraphError> {
        Ok(u32::from_le_bytes(self.take(4)?.try_into().unwrap()))
    }

    fn u64(&mut self) -> Result<u64, GraphError> {
        Ok(u64::from_le_bytes(self.take(8)?.try_into().unwrap()))
    }

    fn f64(&mut self) -> Result<f64, GraphError> {
        Ok(f64::from_le_bytes(self.take(8)?.try_into().unwrap()))
    }
}
