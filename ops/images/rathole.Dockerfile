FROM docker.io/rapiz1/rathole:v0.5.0 AS upstream

FROM debian:bookworm-slim

RUN apt-get update -y \
 && apt-get install -y --no-install-recommends ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && useradd -r -u 1000 -m -d /app rathole

COPY --from=upstream /app/rathole /usr/local/bin/rathole

USER 1000:1000
WORKDIR /app

ENTRYPOINT ["/usr/local/bin/rathole"]
