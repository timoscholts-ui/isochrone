# Multi-stage build for the isochrone server.
#
# Railway's default (Nixpacks) auto-detects Cargo.toml and does NOT need this
# file; this Dockerfile is an alternative for hosts that build from a Dockerfile.
#
# The image expects the precomputed data to be present in the build context:
#   data/            -> drive.graph.bin, walk.graph.bin, buurten.json
#   isochrone_viewer/ -> {slug}_supermarkten.json, {slug}_bag_adressen.json
#
# Build:  docker build -t isochrone-server .
# Run:    docker run -p 8000:8000 isochrone-server

# --- build stage ----------------------------------------------------------
FROM rust:1.85-slim AS builder
WORKDIR /app
COPY . .
RUN cargo build --release -p isochrone-server

# --- runtime stage --------------------------------------------------------
FROM debian:bookworm-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app

COPY --from=builder /app/target/release/isochrone-server /usr/local/bin/isochrone-server
COPY data/ ./data/
COPY isochrone_viewer/ ./isochrone_viewer/

ENV OUT_DIR=/app/data \
    VIEWER_DIR=/app/isochrone_viewer \
    HOST=0.0.0.0 \
    PORT=8000

EXPOSE 8000
CMD ["isochrone-server"]
