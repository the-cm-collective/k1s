"""Declarative specification models for the ae application engine."""

from __future__ import annotations

from pathlib import Path
from typing import List, Literal, Optional

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator


class ManifestError(RuntimeError):
    """Raised when a manifest cannot be parsed."""


class Metadata(BaseModel):
    """Metadata block for top-level resources."""

    name: str


class HTTPGetProbe(BaseModel):
    """HTTP probe configuration."""

    path: str = Field(default="/")
    port: int

class TCPSocketProbe(BaseModel):
    """TCP socket probe configuration."""

    port: int

class ExecProbe(BaseModel):
    """Exec probe: run a command inside the container."""

    command: List[str]


class ProbeSpec(BaseModel):
    """Container probe definition."""

    http_get: Optional[HTTPGetProbe] = Field(default=None, alias="httpGet")
    exec: Optional[ExecProbe] = Field(default=None, alias="exec")
    tcp_socket: Optional[TCPSocketProbe] = Field(default=None, alias="tcpSocket")
    initial_delay_seconds: int = Field(default=0, alias="initialDelaySeconds")
    timeout_seconds: int = Field(default=1, alias="timeoutSeconds")
    period_seconds: int = Field(default=10, alias="periodSeconds")
    success_threshold: int = Field(default=1, alias="successThreshold")
    failure_threshold: int = Field(default=3, alias="failureThreshold")

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


class ServiceSpec(BaseModel):
    """Service abstraction (single-host) providing a stable published port.

    Note: In this initial implementation, `service.port` is only supported when
    replicas == 1. For multi-replica services, the controller will fall back to
    per-replica ephemeral host ports and ingress load-balances to one endpoint.
    """

    port: int = Field(description="Host port to publish (stable)")
    target_port: Optional[int] = Field(
        default=None,
        alias="targetPort",
        description="Container port to expose; defaults to first port",
    )

    model_config = {"populate_by_name": True}


class ResourceQuantities(BaseModel):
    """CPU and memory quantities.

    cpu: float in cores (e.g., 0.5 for half a core)
    memory: string with unit (e.g., "256Mi", "1Gi", "512M")
    """

    cpu: Optional[float] = None
    memory: Optional[str] = None

    @field_validator("cpu")
    @classmethod
    def _cpu_positive(cls, v: Optional[float]):  # noqa: D401 - simple guard
        if v is None:
            return v
        if v <= 0:
            raise ValueError("cpu must be > 0 (cores)")
        return v

    @field_validator("memory")
    @classmethod
    def _mem_format(cls, v: Optional[str]):  # noqa: D401 - simple guard
        if v is None:
            return v
        s = str(v).strip()
        # accept digits only or digits+unit (K/M/G with optional iB/B)
        import re

        pattern = re.compile(r"^\d+(?:\.\d+)?\s*(?:[KMG](?:i?B)?|[kKmMgG]|)$")
        if not pattern.match(s):
            raise ValueError("memory must be a number optionally suffixed by K/M/G or KiB/MiB/GiB")
        return s


class ResourcesSpec(BaseModel):
    """Resource requests and limits (limits used for Docker flags)."""

    requests: Optional[ResourceQuantities] = None
    limits: Optional[ResourceQuantities] = None


class SecuritySpec(BaseModel):
    """Container security context (subset aligned with K8s semantics)."""

    run_as_user: Optional[int] = Field(default=None, alias="runAsUser")
    run_as_group: Optional[int] = Field(default=None, alias="runAsGroup")
    read_only_root: bool = Field(default=False, alias="readOnlyRootFilesystem")
    drop_caps: List[str] = Field(default_factory=list, alias="dropCapabilities")

    model_config = {"populate_by_name": True}


class VolumeSpec(BaseModel):
    """HostPath volume mapping."""

    host_path: str = Field(alias="hostPath")
    mount_path: str = Field(alias="mountPath")
    read_only: bool = Field(default=False, alias="readOnly")

    model_config = {"populate_by_name": True}


class StorageRetention(str):
    Retain = "Retain"
    Delete = "Delete"


class StorageSpec(BaseModel):
    """Named persistent storage volume (PV-lite).

    The controller creates a Docker named volume per entry and mounts it at
    the specified path. Retention controls removal on app deletion.
    """

    name: str
    mount_path: str = Field(alias="mountPath")
    retention: str = Field(default=StorageRetention.Retain)
    size: str | None = None  # reserved for future use

    model_config = {"populate_by_name": True}


class SecretEnvMapping(BaseModel):
    """Mapping from decrypted secret key to environment variable."""

    name: str
    key: str


class SecretRef(BaseModel):
    """Reference to a sealed secret file decrypted at apply time."""

    name: str
    path: str
    env: List[SecretEnvMapping] = Field(default_factory=list)
    # Optional file projections: project selected keys into files under the mount root
    files: List[dict] = Field(default_factory=list)


class ConfigEnvMapping(BaseModel):
    """Mapping from config key to environment variable."""

    name: str
    key: str


class ConfigRef(BaseModel):
    """Reference to a config file (YAML/JSON) projected into env vars."""

    name: str
    path: str
    env: List[ConfigEnvMapping] = Field(default_factory=list)
    # Optional file projections: project selected keys into files under the mount root
    files: List[dict] = Field(default_factory=list)


class AppSpec(BaseModel):
    """Workload specification."""

    image: str
    command: Optional[List[str]] = None
    env: List[dict[str, str]] = Field(default_factory=list)
    replicas: int = Field(default=1, ge=1)
    ports: List[PortSpec] = Field(default_factory=list)
    health: Optional[HealthSpec] = None
    ingress: Optional[IngressSpec] = None
    service: Optional[ServiceSpec] = None
    # Rollout policy
    rollout: Optional[dict] = Field(
        default_factory=lambda: {"strategy": "parallel", "maxSurge": 1, "maxUnavailable": 0}
    )
    registry_auth_ref: Optional[str] = Field(default=None, alias="registryAuthRef")
    secret_refs: List[SecretRef] = Field(default_factory=list, alias="secretRefs")
    config_refs: List[ConfigRef] = Field(default_factory=list, alias="configRefs")
    resources: Optional[ResourcesSpec] = None
    security: Optional[SecuritySpec] = None
    termination_grace_period_seconds: int = Field(default=10, alias="terminationGracePeriodSeconds")
    volumes: List[VolumeSpec] = Field(default_factory=list)
    storage: List[StorageSpec] = Field(default_factory=list)

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
