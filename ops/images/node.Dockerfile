FROM python:3.11-slim

ARG CRICTL_VERSION=1.30.0
ARG TARGETARCH

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update -y \
  && apt-get install -y --no-install-recommends ca-certificates curl tar gzip \
  && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md /app/
COPY src /app/src

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
  rm -f /tmp/crictl.tgz

EXPOSE 9109
VOLUME ["/var/lib/ae", "/var/run/ae"]

ENTRYPOINT ["python", "-m", "ae.node"]
