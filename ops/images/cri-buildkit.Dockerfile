FROM debian:bookworm-slim

ARG BUILDKIT_VERSION=0.13.2
ARG TARGETARCH

RUN apt-get update \
  && apt-get install -y --no-install-recommends ca-certificates curl tar gzip \
  && rm -rf /var/lib/apt/lists/*

RUN set -e; \
  arch="${TARGETARCH:-amd64}"; \
  case "$arch" in \
    amd64|arm64) ;; \
    *) echo "unsupported arch: $arch"; exit 1 ;; \
  esac; \
  curl -fsSL -o /tmp/buildkit.tgz \
    "https://github.com/moby/buildkit/releases/download/v${BUILDKIT_VERSION}/buildkit-v${BUILDKIT_VERSION}.linux-${arch}.tar.gz"; \
  tar -C /usr/local/bin -xzf /tmp/buildkit.tgz --strip-components=1 bin/buildctl bin/buildkitd; \
  rm -f /tmp/buildkit.tgz

ENTRYPOINT ["/bin/sh", "-c"]
CMD ["sleep infinity"]
