//! Single-source Dijkstra over the CSR graph + a small LRU cache mirroring
//! `server.py`'s `_dijkstra_cache` (clear-on-full).

use std::cmp::Ordering;
use std::cmp::Reverse;
use std::collections::{BinaryHeap, HashMap};
use std::sync::{Arc, RwLock};

use crate::graph::Graph;

/// Total-order wrapper for non-negative, finite `f64` distances.
#[derive(PartialEq, Clone, Copy)]
struct Dist(f64);

impl Eq for Dist {}

impl PartialOrd for Dist {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}

impl Ord for Dist {
    fn cmp(&self, other: &Self) -> Ordering {
        self.0
            .partial_cmp(&other.0)
            .unwrap_or(Ordering::Equal)
    }
}

/// Shortest-path distances (minutes) from `src` to every node.
/// Unreachable nodes hold `f64::INFINITY`.
#[must_use]
pub fn dijkstra(graph: &Graph, src: u32) -> Vec<f64> {
    let n = graph.node_count();
    let mut dist = vec![f64::INFINITY; n];
    dist[src as usize] = 0.0;
    let mut heap: BinaryHeap<Reverse<(Dist, u32)>> = BinaryHeap::new();
    heap.push(Reverse((Dist(0.0), src)));
    while let Some(Reverse((Dist(d), u))) = heap.pop() {
        if d > dist[u as usize] {
            continue;
        }
        for (v, w) in graph.outgoing(u) {
            let nd = d + w;
            if nd < dist[v as usize] {
                dist[v as usize] = nd;
                heap.push(Reverse((Dist(nd), v)));
            }
        }
    }
    dist
}

/// Thread-safe LRU-ish cache of Dijkstra results, keyed by `(name, node)`.
/// Mirrors Python's clear-on-full semantics.
///
/// Uses `RwLock` so concurrent cache *hits* (reads) do not block each other —
/// only a cache miss needs an exclusive write lock.
pub struct DijkstraCache {
    inner: RwLock<HashMap<(String, u32), Arc<Vec<f64>>>>,
    max: usize,
}

impl DijkstraCache {
    #[must_use]
    pub fn new(max: usize) -> Self {
        Self {
            inner: RwLock::new(HashMap::new()),
            max,
        }
    }

    /// Returns the cached distances for `(name, src)`, computing them on miss.
    pub fn get_or_compute(&self, graph: &Graph, name: &str, src: u32) -> Arc<Vec<f64>> {
        let key = (name.to_string(), src);

        // Fast path: shared read lock — many threads can hit this concurrently.
        {
            let g = self.inner.read().unwrap();
            if let Some(v) = g.get(&key) {
                return Arc::clone(v);
            }
        }

        // Slow path: compute Dijkstra outside any lock, then write.
        let d = Arc::new(dijkstra(graph, src));
        let mut g = self.inner.write().unwrap();
        // Double-check: another thread may have inserted this key while we computed.
        if let Some(v) = g.get(&key) {
            return Arc::clone(v);
        }
        if g.len() >= self.max {
            g.clear();
        }
        g.insert(key, Arc::clone(&d));
        d
    }
}
