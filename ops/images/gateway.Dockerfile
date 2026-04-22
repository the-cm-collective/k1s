FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update -y && apt-get install -y --no-install-recommends \
    ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md requirements.in /app/
COPY src /app/src

RUN pip install --no-cache-dir .

VOLUME ["/var/lib/ae", "/var/run/ae"]

ENV AE_GATEWAY_SPOOL_PATH=/var/lib/ae/gateway.db

ENTRYPOINT ["python", "-m", "ae.gateway"]
