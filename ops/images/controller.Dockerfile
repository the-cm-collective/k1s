FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install runtime deps
RUN apt-get update -y && apt-get install -y --no-install-recommends \
    ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Copy project
COPY pyproject.toml README.md /app/
COPY src /app/src

# Install package
RUN pip install --no-cache-dir .

# Expose default API port (override via args)
EXPOSE 9108

# Default specs directory; mount host path at runtime
VOLUME ["/specs", "/state", "/var/run/ae"]

# Entrypoint: controller with API enabled; pass extra args via CMD
ENTRYPOINT ["python", "-m", "ae.controller", "--loop", "--specs", "/specs", "--metrics-port", "9108"]
