"""Declarative specification models for the ae application engine."""

from __future__ import annotations

from pathlib import Path
from typing import List, Literal, Optional

import yaml
from pydantic import BaseModel, Field, ValidationError


class ManifestError(RuntimeError):
    """Raised when a manifest cannot be parsed."""


class Metadata(BaseModel):
    """Metadata block for top-level resources."""

    name: str


class HTTPGetProbe(BaseModel):
    """HTTP probe configuration."""

    path: str = Field(default="/")
    port: int


class ProbeSpec(BaseModel):
    """Container probe definition."""

    http_get: Optional[HTTPGetProbe] = Field(default=None, alias="httpGet")
    initial_delay_seconds: int = Field(default=0, alias="initialDelaySeconds")
    timeout_seconds: int = Field(default=1, alias="timeoutSeconds")

    model_config = {"populate_by_name": True}


class PortSpec(BaseModel):
    """Container port definition."""

    name: str
    container_port: int = Field(alias="containerPort")

    model_config = {"populate_by_name": True}


class HealthSpec(BaseModel):
    """Readiness and liveness probes."""

    readiness: Optional[ProbeSpec] = None
    liveness: Optional[ProbeSpec] = None


class IngressSpec(BaseModel):
    """Ingress configuration targeting Caddy/nginx."""

    host: str
    path: str = Field(default="/")
    tls: bool = Field(default=True)


class AppSpec(BaseModel):
    """Workload specification."""

    image: str
    command: Optional[List[str]] = None
    env: List[dict[str, str]] = Field(default_factory=list)
    replicas: int = Field(default=1, ge=1)
    ports: List[PortSpec] = Field(default_factory=list)
    health: Optional[HealthSpec] = None
    ingress: Optional[IngressSpec] = None
    registry_auth_ref: Optional[str] = Field(default=None, alias="registryAuthRef")

    model_config = {"populate_by_name": True}


class AppManifest(BaseModel):
    """Top-level application manifest."""

    api_version: Literal["ae.dev/v1alpha1"] = Field(alias="apiVersion")
    kind: Literal["App"]
    metadata: Metadata
    spec: AppSpec

    model_config = {"populate_by_name": True}


def load_manifest(path: Path) -> AppManifest:
    """Load an App manifest from YAML."""

    try:
        data = yaml.safe_load(path.read_text())
    except FileNotFoundError as exc:
        raise ManifestError(f"Manifest {path} not found") from exc
    except yaml.YAMLError as exc:
        raise ManifestError(f"Failed to parse YAML for manifest {path}: {exc}") from exc

    if not isinstance(data, dict):
        raise ManifestError(f"Manifest {path} must be a YAML mapping")

    try:
        return AppManifest.model_validate(data)
    except ValidationError as exc:
        raise ManifestError(f"Manifest {path} failed validation: {exc}") from exc
