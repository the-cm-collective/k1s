ARG BASE_IMAGE=python:3.12-alpine
FROM ${BASE_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml requirements.txt requirements.in README.md /app/
COPY src /app/src

RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir .

VOLUME ["/var/lib/ae"]

ENTRYPOINT ["python", "-m", "ae.gateway"]
