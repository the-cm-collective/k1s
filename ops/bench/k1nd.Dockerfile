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
