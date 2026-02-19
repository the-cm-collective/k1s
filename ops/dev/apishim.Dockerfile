FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends podman \
    && rm -rf /var/lib/apt/lists/*

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

# Pre-install ae and its dependencies at image build time so container start
# does not depend on live pip resolution.
COPY pyproject.toml README.md LICENSE requirements.in /tmp/ae-src/
COPY src /tmp/ae-src/src
RUN python -m pip install --upgrade pip setuptools wheel \
    && python -m pip install /tmp/ae-src

WORKDIR /workspace
