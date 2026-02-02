FROM debian:bookworm-slim

ARG CRICTL_VERSION=1.30.0
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
  curl -fsSL -o /tmp/crictl.tgz \
    "https://github.com/kubernetes-sigs/cri-tools/releases/download/v${CRICTL_VERSION}/crictl-v${CRICTL_VERSION}-linux-${arch}.tar.gz"; \
  tar -C /usr/local/bin -xzf /tmp/crictl.tgz; \
  rm -f /tmp/crictl.tgz

ENTRYPOINT ["/bin/sh", "-c"]
CMD ["sleep infinity"]
