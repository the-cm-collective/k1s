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

EXPOSE 8445
VOLUME ["/state", "/etc/ae"]

ENTRYPOINT ["python", "-m", "ae.apishim", "serve", "--host", "127.0.0.1", "--port", "8445", "--tls"]
