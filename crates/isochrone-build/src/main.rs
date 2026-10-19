//! PBF → graph.bin builder (Phase B). Ports the network-loading half of
//! `build_focus_isochrones.py` (`load_networks` / `_osm_network` / `add_drive_time`
//! / `_set_time_minutes`) to Rust via the `osmpbf` crate.
//!
//! Streams a Geofabrik `.pbf`, bbox-clips to the region + margin, applies the
//! drive / walk highway filter, builds directed edges (haversine length, speed,
//! time), simplifies degree-2 chains, keeps the largest weakly-connected
//! component, and writes the ISOG `*.graph.bin` format the server loads.

use std::collections::{HashMap, HashSet};
use std::path::PathBuf;

use clap::Parser;
use osmpbf::{Element, ElementReader};
use tracing::info;

use isochrone_core::crs;

const EARTH_RADIUS_M: f64 = 6_371_009.0;
const DRIVE_FALLBACK_KMH: f64 = 50.0;
const WALK_SPEED_KMH: f64 = 5.0;

/// Highway values excluded from the drive network (pyrosm `driving_filter`).
const DRIVE_EXCLUDED: &[&str] = &[
    "abandoned", "construction", "no", "planned", "platform", "proposed", "raceway", "razed",
    "rest_area", "services", "bridleway", "bus_guideway", "corridor", "cycleway", "elevator",
    "escalator", "footway", "path", "pedestrian", "steps", "track", "service",
];

/// Highway values excluded from the walk network (pyrosm `walking_filter` with
/// `cycleway` re-included per `_walk_cycle_filter`).
const WALK_EXCLUDED: &[&str] = &[
    "abandoned", "construction", "no", "planned", "platform", "proposed", "raceway", "razed",
    "rest_area", "services", "bus_guideway", "motor", "motorway", "motorway_link",
];

const DRIVE_SERVICE_EXCLUDED: &[&str] = &[
    "emergency_access", "parking", "parking_aisle", "private", "alley", "driveway",
];

#[derive(Parser, Debug)]
#[command(name = "isochrone-build", version, about)]
struct Args {
    /// Input Geofabrik .osm.pbf.
    #[arg(long, env = "ISO_PBF")]
    pbf: PathBuf,

    /// Output directory (writes drive.graph.bin / walk.graph.bin).
    #[arg(long, env = "OUT_DIR", default_value = "data")]
    out_dir: PathBuf,

    /// Network to build: "drive" or "walk".
    #[arg(long, default_value = "drive")]
    network: String,

    /// Region bounding box in RD (EPSG:28992): "minx,miny,maxx,maxy".
    #[arg(long, env = "ISO_BBOX")]
    bbox: String,

    /// Buffer margin around the region (m).
    #[arg(long, default_value = "10000")]
    margin: f64,
}

/// A way that passed the filter, with the tags needed for routing.
struct WayInfo {
    refs: Vec<i64>,
    highway: String,
    oneway: Option<String>,
    maxspeed: Option<String>,
}

/// A directed edge (dense node indices) with its travel attributes.
#[derive(Clone, Copy)]
struct Edge {
    from: usize,
    to: usize,
    length: f64,
    speed: f64,
    time: f64,
}

fn main() -> anyhow::Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| tracing_subscriber::EnvFilter::new("info")),
        )
        .init();

    let args = Args::parse();
    let is_drive = args.network == "drive";
    let bbox_rd = parse_bbox(&args.bbox)?;

    let (west, south, east, north) = wgs84_bbox(bbox_rd, args.margin);
    info!(
        "[build] {} netwerk: WGS84 bbox west={:.4} south={:.4} east={:.4} north={:.4}",
        args.network, west, south, east, north
    );

    let (node_coords, ways) = read_pbf(&args.pbf, west, south, east, north, is_drive)?;
    info!(
        "[build] {} nodes in bbox, {} ways na filter",
        node_coords.len(),
        ways.len()
    );

    let (nodes, edges) = build_graph(node_coords, ways, is_drive);
    info!(
        "[build] raw: {} nodes, {} edges",
        nodes.len(),
        edges.len()
    );

    let simplified = simplify(&nodes, &edges);
    info!(
        "[build] simplify: {} nodes, {} edges -> {} nodes, {} edges",
        nodes.len(),
        edges.len(),
        simplified_nodes(&simplified).len(),
        simplified.len()
    );

    let (kept_nodes, kept_edges) = largest_component(&nodes, &simplified);
    info!(
        "[build] largest component: {} nodes, {} edges",
        kept_nodes.len(),
        kept_edges.len()
    );

    let out = args.out_dir.join(format!("{}.graph.bin", args.network));
    write_isog(&out, &kept_nodes, &kept_edges)?;
    info!("[build] wrote {}", out.display());
    Ok(())
}

fn parse_bbox(s: &str) -> anyhow::Result<(f64, f64, f64, f64)> {
    let v: Vec<f64> = s
        .split(',')
        .map(|x| x.trim().parse())
        .collect::<Result<_, _>>()?;
    anyhow::ensure!(v.len() == 4, "bbox must be minx,miny,maxx,maxy");
    Ok((v[0], v[1], v[2], v[3]))
}

fn wgs84_bbox((minx, miny, maxx, maxy): (f64, f64, f64, f64), margin: f64) -> (f64, f64, f64, f64) {
    let corners = [
        (minx - margin, miny - margin),
        (minx - margin, maxy + margin),
        (maxx + margin, miny - margin),
        (maxx + margin, maxy + margin),
    ];
    let mut west = f64::INFINITY;
    let mut south = f64::INFINITY;
    let mut east = f64::NEG_INFINITY;
    let mut north = f64::NEG_INFINITY;
    for (x, y) in corners {
        let (lon, lat) = crs::to_wgs84(x, y);
        west = west.min(lon);
        south = south.min(lat);
        east = east.max(lon);
        north = north.max(lat);
    }
    (west, south, east, north)
}

fn read_pbf(
    path: &std::path::Path,
    west: f64,
    south: f64,
    east: f64,
    north: f64,
    is_drive: bool,
) -> anyhow::Result<(HashMap<i64, (f64, f64)>, Vec<WayInfo>)> {
    let reader = ElementReader::from_path(path)?;
    let mut node_coords: HashMap<i64, (f64, f64)> = HashMap::new();
    let mut ways: Vec<WayInfo> = Vec::new();

    reader.for_each(|element| {
        match element {
            Element::Node(node) => {
                let lon = node.lon();
                let lat = node.lat();
                if lon >= west && lon <= east && lat >= south && lat <= north {
                    node_coords.insert(node.id(), (lon, lat));
                }
            }
            Element::DenseNode(node) => {
                let lon = node.lon();
                let lat = node.lat();
                if lon >= west && lon <= east && lat >= south && lat <= north {
                    node_coords.insert(node.id(), (lon, lat));
                }
            }
            Element::Way(way) => {
                if let Some(info) = way_info(&way, is_drive) {
                    ways.push(info);
                }
            }
            Element::Relation(_) => {}
        }
    })?;

    Ok((node_coords, ways))
}

fn way_info(way: &osmpbf::Way<'_>, is_drive: bool) -> Option<WayInfo> {
    let mut highway = None;
    let mut area = None;
    let mut service = None;
    let mut access = None;
    let mut motorcar = None;
    let mut motor_vehicle = None;
    let mut vehicle = None;
    let mut foot = None;
    let mut oneway = None;
    let mut maxspeed = None;

    for (k, v) in way.tags() {
        match k {
            "highway" => highway = Some(v.to_string()),
            "area" => area = Some(v),
            "service" => service = Some(v),
            "access" => access = Some(v),
            "motorcar" => motorcar = Some(v),
            "motor_vehicle" => motor_vehicle = Some(v),
            "vehicle" => vehicle = Some(v),
            "foot" => foot = Some(v),
            "oneway" => oneway = Some(v.to_string()),
            "maxspeed" => maxspeed = Some(v.to_string()),
            _ => {}
        }
    }

    let highway = highway?;
    if area == Some("yes") {
        return None;
    }

    if is_drive {
        if DRIVE_EXCLUDED.contains(&highway.as_str()) {
            return None;
        }
        if service.is_some_and(|s| DRIVE_SERVICE_EXCLUDED.contains(&s)) {
            return None;
        }
        for v in [access, motorcar, motor_vehicle, vehicle] {
            if matches!(v, Some("no") | Some("private")) {
                return None;
            }
        }
    } else {
        if WALK_EXCLUDED.contains(&highway.as_str()) {
            return None;
        }
        if matches!(foot, Some("no") | Some("private")) {
            return None;
        }
        if service == Some("private") {
            return None;
        }
    }

    let refs: Vec<i64> = way.refs().collect();
    if refs.len() < 2 {
        return None;
    }
    Some(WayInfo {
        refs,
        highway,
        oneway,
        maxspeed,
    })
}

fn haversine(lon1: f64, lat1: f64, lon2: f64, lat2: f64) -> f64 {
    let dlat = (lat2 - lat1).to_radians();
    let dlon = (lon2 - lon1).to_radians();
    let a = (dlat / 2.0).sin().powi(2)
        + lat1.to_radians().cos() * lat2.to_radians().cos() * (dlon / 2.0).sin().powi(2);
    2.0 * EARTH_RADIUS_M * a.sqrt().asin()
}

fn parse_maxspeed(ms: Option<&str>) -> f64 {
    let s = match ms {
        None => return DRIVE_FALLBACK_KMH,
        Some(s) => s.trim().to_lowercase(),
    };
    let num_start = s
        .find(|c: char| c.is_ascii_digit())
        .unwrap_or(s.len());
    let num: f64 = s[num_start..]
        .split(|c: char| !c.is_ascii_digit() && c != '.')
        .next()
        .and_then(|t| t.parse().ok())
        .unwrap_or(DRIVE_FALLBACK_KMH);
    if s.contains("mph") {
        num * 1.60934
    } else {
        num
    }
}

fn build_graph(
    node_coords: HashMap<i64, (f64, f64)>,
    ways: Vec<WayInfo>,
    is_drive: bool,
) -> (Vec<Node>, Vec<Edge>) {
    // Only keep nodes referenced by edges; project to RD.
    let mut node_ids: Vec<i64> = Vec::new();
    let mut id_to_idx: HashMap<i64, usize> = HashMap::new();

    let mut edges: Vec<Edge> = Vec::new();
    for w in &ways {
        let oneway = oneway_kind(&w.highway, w.oneway.as_deref());
        let speed = if is_drive {
            parse_maxspeed(w.maxspeed.as_deref())
        } else {
            WALK_SPEED_KMH
        };
        for pair in w.refs.windows(2) {
            let (a, b) = (pair[0], pair[1]);
            let (lon1, lat1) = match node_coords.get(&a) {
                Some(c) => *c,
                None => continue,
            };
            let (lon2, lat2) = match node_coords.get(&b) {
                Some(c) => *c,
                None => continue,
            };
            let length = haversine(lon1, lat1, lon2, lat2);
            let time = length / (speed * 1000.0 / 60.0);

            let ai = *id_to_idx.entry(a).or_insert_with(|| {
                let i = node_ids.len();
                node_ids.push(a);
                i
            });
            let bi = *id_to_idx.entry(b).or_insert_with(|| {
                let i = node_ids.len();
                node_ids.push(b);
                i
            });

            match oneway {
                Oneway::Forward => edges.push(Edge { from: ai, to: bi, length, speed, time }),
                Oneway::Reverse => edges.push(Edge { from: bi, to: ai, length, speed, time }),
                Oneway::Both => {
                    edges.push(Edge { from: ai, to: bi, length, speed, time });
                    edges.push(Edge { from: bi, to: ai, length, speed, time });
                }
            }
        }
    }

    let nodes: Vec<Node> = node_ids
        .iter()
        .map(|&id| {
            let (lon, lat) = node_coords[&id];
            let (x, y) = crs::to_rd(lon, lat);
            Node { id, x, y }
        })
        .collect();
    (nodes, edges)
}

#[derive(Clone, Copy)]
enum Oneway {
    Forward,
    Reverse,
    Both,
}

fn oneway_kind(highway: &str, oneway: Option<&str>) -> Oneway {
    match oneway.map(str::to_lowercase).as_deref() {
        Some("yes") | Some("true") | Some("1") => Oneway::Forward,
        Some("-1") | Some("reverse") => Oneway::Reverse,
        Some("no") | Some("false") | Some("0") => Oneway::Both,
        _ => {
            if highway == "motorway" || highway == "motorway_link" {
                Oneway::Forward
            } else {
                Oneway::Both
            }
        }
    }
}

/// A node with its RD coordinates and OSM id.
#[derive(Clone)]
struct Node {
    id: i64,
    x: f64,
    y: f64,
}

// ---------------------------------------------------------------------------
// Simplification: collapse undirected degree-2 chains.
// ---------------------------------------------------------------------------
#[derive(Clone, Copy)]
struct Seg {
    length: f64,
    speed: f64,
}

#[derive(Clone)]
struct Link {
    a: usize,
    b: usize,
    fwd: Option<Seg>, // a -> b
    bwd: Option<Seg>, // b -> a
}

fn combine(x: Option<Seg>, y: Option<Seg>) -> Option<Seg> {
    match (x, y) {
        (Some(p), Some(q)) => Some(Seg {
            length: p.length + q.length,
            speed: p.speed.min(q.speed),
        }),
        _ => None,
    }
}

/// Accumulate parallel segments (None acts as identity — the first segment wins).
fn merge_seg(acc: Option<Seg>, seg: Seg) -> Option<Seg> {
    match acc {
        None => Some(seg),
        Some(p) => Some(Seg {
            length: p.length.min(seg.length),
            speed: p.speed.min(seg.speed),
        }),
    }
}

fn seg_forward(link: &Link, from: usize, to: usize) -> Option<Seg> {
    if link.a == from && link.b == to {
        link.fwd
    } else if link.a == to && link.b == from {
        link.bwd
    } else {
        None
    }
}

fn simplify(nodes: &[Node], edges: &[Edge]) -> Vec<Edge> {
    let n = nodes.len();
    // Build undirected links from directed edges.
    let mut adj: Vec<HashSet<usize>> = vec![HashSet::new(); n];
    let mut links: Vec<Link> = Vec::new();
    let mut pair_to_link: HashMap<(usize, usize), usize> = HashMap::new();

    for e in edges {
        let (a, b) = (e.from.min(e.to), e.from.max(e.to));
        let idx = *pair_to_link.entry((a, b)).or_insert_with(|| {
            let i = links.len();
            links.push(Link { a, b, fwd: None, bwd: None });
            adj[a].insert(i);
            adj[b].insert(i);
            i
        });
        // Set fwd/bwd: fwd is a -> b.
        let link = &mut links[idx];
        let seg = Seg { length: e.length, speed: e.speed };
        if e.from == a {
            link.fwd = merge_seg(link.fwd, seg);
        } else {
            link.bwd = merge_seg(link.bwd, seg);
        }
    }

    // Iteratively collapse nodes with undirected degree 2 (distinct neighbours).
    let mut active: Vec<bool> = vec![true; n];
    loop {
        let mut collapsed = false;
        for u in 0..n {
            if !active[u] || adj[u].len() != 2 {
                continue;
            }
            let mut it = adj[u].iter().copied();
            let l1 = it.next().unwrap();
            let l2 = it.next().unwrap();
            let n1 = if links[l1].a == u { links[l1].b } else { links[l1].a };
            let n2 = if links[l2].a == u { links[l2].b } else { links[l2].a };
            if n1 == n2 {
                continue; // parallel-link lollipop; leave as-is
            }
            let fwd = combine(seg_forward(&links[l1], n1, u), seg_forward(&links[l2], u, n2));
            let bwd = combine(seg_forward(&links[l2], n2, u), seg_forward(&links[l1], u, n1));

            // Remove l1, l2 from adjacency.
            adj[n1].remove(&l1);
            adj[n1].remove(&l2);
            adj[n2].remove(&l1);
            adj[n2].remove(&l2);
            active[u] = false;
            adj[u].clear();

            // Add merged link between n1, n2.
            let (a, b) = (n1.min(n2), n1.max(n2));
            let idx = *pair_to_link.entry((a, b)).or_insert_with(|| {
                let i = links.len();
                links.push(Link { a, b, fwd: None, bwd: None });
                adj[a].insert(i);
                adj[b].insert(i);
                i
            });
            let link = &mut links[idx];
            if n1 == a {
                if let Some(s) = fwd {
                    link.fwd = merge_seg(link.fwd, s);
                }
                if let Some(s) = bwd {
                    link.bwd = merge_seg(link.bwd, s);
                }
            } else {
                if let Some(s) = fwd {
                    link.bwd = merge_seg(link.bwd, s);
                }
                if let Some(s) = bwd {
                    link.fwd = merge_seg(link.fwd, s);
                }
            }
            collapsed = true;
        }
        if !collapsed {
            break;
        }
    }

    // Reconstruct directed edges from links.
    let mut out = Vec::new();
    for link in &links {
        if !active[link.a] || !active[link.b] {
            continue;
        }
        if let Some(fwd) = link.fwd {
            out.push(Edge {
                from: link.a,
                to: link.b,
                length: fwd.length,
                speed: fwd.speed,
                time: fwd.length / (fwd.speed * 1000.0 / 60.0),
            });
        }
        if let Some(bwd) = link.bwd {
            out.push(Edge {
                from: link.b,
                to: link.a,
                length: bwd.length,
                speed: bwd.speed,
                time: bwd.length / (bwd.speed * 1000.0 / 60.0),
            });
        }
    }
    out
}

/// Number of distinct active nodes referenced by the simplified edges.
fn simplified_nodes(edges: &[Edge]) -> HashSet<usize> {
    let mut s = HashSet::new();
    for e in edges {
        s.insert(e.from);
        s.insert(e.to);
    }
    s
}

// ---------------------------------------------------------------------------
// Largest weakly-connected component (union-find over undirected edges).
// ---------------------------------------------------------------------------
fn largest_component(nodes: &[Node], edges: &[Edge]) -> (Vec<Node>, Vec<Edge>) {
    let n = nodes.len();
    let mut parent: Vec<usize> = (0..n).collect();
    fn find(parent: &mut [usize], x: usize) -> usize {
        let mut root = x;
        while parent[root] != root {
            root = parent[root];
        }
        let mut y = x;
        while parent[y] != y {
            let next = parent[y];
            parent[y] = root;
            y = next;
        }
        root
    }
    for e in edges {
        let (ra, rb) = (find(&mut parent, e.from), find(&mut parent, e.to));
        if ra != rb {
            parent[ra] = rb;
        }
    }
    let mut size = vec![0usize; n];
    for i in 0..n {
        size[find(&mut parent, i)] += 1;
    }
    let largest = (0..n).max_by_key(|&i| size[i]).unwrap_or(0);
    let root = find(&mut parent, largest);

    let mut kept: Vec<bool> = vec![false; n];
    for i in 0..n {
        kept[i] = find(&mut parent, i) == root;
    }
    // Remap old node indices to dense positions in the filtered node list.
    let mut remap = vec![usize::MAX; n];
    let mut nodes_out: Vec<Node> = Vec::new();
    for (i, nd) in nodes.iter().enumerate() {
        if kept[i] {
            remap[i] = nodes_out.len();
            nodes_out.push(nd.clone());
        }
    }
    let edges_out: Vec<Edge> = edges
        .iter()
        .filter(|e| kept[e.from] && kept[e.to])
        .map(|e| Edge {
            from: remap[e.from],
            to: remap[e.to],
            ..*e
        })
        .collect();
    (nodes_out, edges_out)
}

// ---------------------------------------------------------------------------
// ISOG writer
// ---------------------------------------------------------------------------
fn write_isog(path: &std::path::Path, nodes: &[Node], edges: &[Edge]) -> anyhow::Result<()> {
    use std::io::Write;
    let mut buf = Vec::with_capacity(nodes.len() * 24 + edges.len() * 40 + 24);
    buf.extend_from_slice(b"ISOG");
    buf.extend_from_slice(&1u32.to_le_bytes());
    buf.extend_from_slice(&(nodes.len() as u64).to_le_bytes());
    buf.extend_from_slice(&(edges.len() as u64).to_le_bytes());
    for nd in nodes {
        buf.extend_from_slice(&(nd.id as u64).to_le_bytes());
        buf.extend_from_slice(&nd.x.to_le_bytes());
        buf.extend_from_slice(&nd.y.to_le_bytes());
    }
    for e in edges {
        buf.extend_from_slice(&(nodes[e.from].id as u64).to_le_bytes());
        buf.extend_from_slice(&(nodes[e.to].id as u64).to_le_bytes());
        buf.extend_from_slice(&e.length.to_le_bytes());
        buf.extend_from_slice(&e.time.to_le_bytes());
        buf.extend_from_slice(&e.speed.to_le_bytes());
    }
    let mut f = std::fs::File::create(path)?;
    f.write_all(&buf)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn node(id: i64) -> Node {
        Node { id, x: id as f64, y: 0.0 }
    }

    fn edge(from: usize, to: usize) -> Edge {
        Edge { from, to, length: 1.0, speed: 5.0, time: 0.0 }
    }

    #[test]
    fn simplify_chain() {
        // 0 -- 1 -- 2  bidirectional chain; node 1 has degree 2 and should collapse.
        let nodes = vec![node(0), node(1), node(2)];
        let edges = vec![edge(0, 1), edge(1, 0), edge(1, 2), edge(2, 1)];
        let out = simplify(&nodes, &edges);
        eprintln!("simplify_chain -> {} edges: {:?}", out.len(), out.iter().map(|e| (e.from, e.to)).collect::<Vec<_>>());
        assert_eq!(out.len(), 2);
        assert!(out.iter().any(|e| e.from == 0 && e.to == 2));
        assert!(out.iter().any(|e| e.from == 2 && e.to == 0));
    }

    #[test]
    fn simplify_keeps_junction() {
        // 0 -- 1 -- 2 and 1 -- 3  (node 1 has degree 3 -> kept).
        let nodes = vec![node(0), node(1), node(2), node(3)];
        let edges = vec![
            edge(0, 1), edge(1, 0),
            edge(1, 2), edge(2, 1),
            edge(1, 3), edge(3, 1),
        ];
        let out = simplify(&nodes, &edges);
        eprintln!("simplify_junction -> {} edges: {:?}", out.len(), out.iter().map(|e| (e.from, e.to)).collect::<Vec<_>>());
        assert_eq!(out.len(), 6);
    }
}
