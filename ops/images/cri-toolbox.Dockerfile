FROM debian:bookworm-slim

ARG CRICTL_VERSION=1.30.0
ARG NERDCTL_VERSION=1.7.6
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
  curl -fsSL -o /tmp/crictl.tgz \
    "https://github.com/kubernetes-sigs/cri-tools/releases/download/v${CRICTL_VERSION}/crictl-v${CRICTL_VERSION}-linux-${arch}.tar.gz"; \
  tar -C /usr/local/bin -xzf /tmp/crictl.tgz; \
  rm -f /tmp/crictl.tgz; \
  curl -fsSL -o /tmp/nerdctl.tgz \
    "https://github.com/containerd/nerdctl/releases/download/v${NERDCTL_VERSION}/nerdctl-${NERDCTL_VERSION}-linux-${arch}.tar.gz"; \
  tar -C /usr/local/bin -xzf /tmp/nerdctl.tgz; \
  rm -f /tmp/nerdctl.tgz; \
  curl -fsSL -o /tmp/buildkit.tgz \
    "https://github.com/moby/buildkit/releases/download/v${BUILDKIT_VERSION}/buildkit-v${BUILDKIT_VERSION}.linux-${arch}.tar.gz"; \
  tar -C /usr/local/bin -xzf /tmp/buildkit.tgz --strip-components=1 bin/buildctl bin/buildkitd; \
  rm -f /tmp/buildkit.tgz

ENTRYPOINT ["/bin/sh", "-c"]
CMD ["sleep infinity"]
