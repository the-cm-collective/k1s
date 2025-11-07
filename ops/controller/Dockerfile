ARG BASE_IMAGE=python:3.12-slim
FROM ${BASE_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install runtime deps and project
COPY pyproject.toml requirements.txt requirements.in README.md /app/
COPY src /app/src

RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir . \
    && useradd -m -u 10001 ae || adduser -D -u 10001 ae

USER 10001:10001

VOLUME ["/etc/ae/specs", "/var/lib/ae"]
EXPOSE 9108

ENV AE_SPECS_DIR=/etc/ae/specs \
    AE_STATE_DB=/var/lib/ae/controller.db

CMD ["python", "-m", "ae.controller", "--loop", "--specs", "/etc/ae/specs", "--metrics-port", "9108", "--watch"]

