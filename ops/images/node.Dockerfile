FROM python:3.11-slim

ARG CRICTL_VERSION=1.30.0
ARG NERDCTL_VERSION=2.2.2
ARG TARGETARCH

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update -y \
  && apt-get install -y --no-install-recommends ca-certificates containerd containernetworking-plugins curl runc tar gzip \
  && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md requirements.in /app/
COPY src /app/src
COPY ops/images/node-entrypoint.sh /usr/local/bin/k1s-node-entrypoint

RUN pip install --no-cache-dir .

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
  tar -C /usr/local/bin -xzf /tmp/nerdctl.tgz nerdctl; \
  chmod 0755 /usr/local/bin/k1s-node-entrypoint; \
  rm -f /tmp/nerdctl.tgz

EXPOSE 9109
VOLUME ["/var/lib/ae", "/var/run/ae"]

ENTRYPOINT ["/usr/local/bin/k1s-node-entrypoint"]
