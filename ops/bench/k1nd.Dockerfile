FROM caddy:2.8 AS caddy

FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        ca-certificates \
        curl \
        docker.io \
        openssl \
    && rm -rf /var/lib/apt/lists/*

ARG DOCKER_CLI_VERSION=26.1.5
RUN set -e; \
    install_ver=""; \
    for v in "${DOCKER_CLI_VERSION}" 26.1.4 26.1.3 26.1.2 26.1.1 26.1.0 26.0.2 26.0.1 26.0.0 25.0.5; do \
      url="https://download.docker.com/linux/static/stable/x86_64/docker-${v}.tgz"; \
      if curl -fsSL "$url" -o /tmp/docker.tgz; then \
        if tar -xzf /tmp/docker.tgz -C /usr/local/bin --strip-components=1 docker/docker; then \
          install_ver="$v"; \
          break; \
        fi; \
      fi; \
    done; \
    rm -f /tmp/docker.tgz; \
    if [ -z "$install_ver" ]; then \
      echo "failed to download docker cli (tried ${DOCKER_CLI_VERSION} and fallbacks)" >&2; \
      exit 1; \
    fi; \
    echo "installed docker cli ${install_ver}"

WORKDIR /workspace

COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY src ./src
COPY ops/dev/caddy /etc/caddy
COPY scripts/ensure_apishim_env.sh ./scripts/ensure_apishim_env.sh
COPY ops/bench/k1nd-entrypoint.sh ./ops/bench/k1nd-entrypoint.sh
COPY --from=caddy /usr/bin/caddy /usr/bin/caddy

RUN mkdir -p /srv/docs

ENV PYTHONPATH=/workspace/src

ENTRYPOINT ["/workspace/ops/bench/k1nd-entrypoint.sh"]
