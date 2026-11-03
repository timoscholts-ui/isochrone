# ── Stage 1: compile ────────────────────────────────────────────────────────
FROM rust:1.81-bookworm AS builder

WORKDIR /build

# Cache dependency compilation separately from source changes.
COPY Cargo.toml Cargo.lock ./
COPY crates/ crates/
RUN cargo build --release --bin isochrone-server

# ── Stage 2: minimal runtime ─────────────────────────────────────────────────
FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y ca-certificates && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Server binary
COPY --from=builder /build/target/release/isochrone-server /app/

# Pre-built graph data (drive.graph.bin, walk.graph.bin, buurten.json)
COPY data/ /app/data/

# Supermarkt JSON files for KPI
COPY viewer_data/ /app/viewer_data/

# Static frontend
COPY index.html /app/

CMD /app/isochrone-server \
    --out-dir /app/data \
    --viewer-dir /app/viewer_data \
    --static-dir /app \
    --port "${PORT:-8000}"
