# ruff: noqa: E501,UP006,UP007,UP017
"""Declarative specification models for the ae application engine."""

from __future__ import annotations

import re
from pathlib import Path
from typing import List, Literal, Optional

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator


DEFAULT_NAMESPACE = "default"
_SHA256_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_AI_MAX_INSTALLER_PATHS = {"gateway", "cell-node"}
_AI_MAX_INSTALLER_ARTIFACT_DIGEST = (
    "sha256:1111111111111111111111111111111111111111111111111111111111111111"
)
_AI_MAX_INSTALLER_MANIFEST_DIGEST = (
    "sha256:2222222222222222222222222222222222222222222222222222222222222222"
)
_AI_MAX_INSTALLER_SIGNATURE = (
    "k1s-sim-signature:3333333333333333333333333333333333333333333333333333333333333333"
)
_AI_MAX_GATEWAY_BOOT_MEASUREMENT_DIGEST = (
    "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
)
_AI_MAX_CELL_NODE_BOOT_MEASUREMENT_DIGEST = (
    "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
)
_AI_MAX_GATEWAY_BOOT_NONCE = "k1s-stage9-nonce-gateway"
_AI_MAX_CELL_NODE_BOOT_NONCE = "k1s-stage9-nonce-cell-node"
_AI_MAX_BOOT_EVIDENCE_CREATED_AT = "2026-06-25T00:00:00Z"


class ManifestError(RuntimeError):
    """Raised when a manifest cannot be parsed."""


class Metadata(BaseModel):
    """Metadata block for top-level resources."""

    name: str
    namespace: Optional[str] = Field(default=DEFAULT_NAMESPACE)
    labels: dict | None = None
    annotations: dict | None = None

    @field_validator("namespace", mode="before")
    @classmethod
    def _normalize_namespace(cls, v: Optional[str]):  # noqa: D401 - simple guard
        if v is None:
            return v
        ns = str(v).strip()
        return ns or None


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


class LifecycleHandler(BaseModel):
    """Container lifecycle handler (exec/http/tcp)."""

    http_get: Optional[HTTPGetProbe] = Field(default=None, alias="httpGet")
    exec: Optional[ExecProbe] = Field(default=None, alias="exec")
    tcp_socket: Optional[TCPSocketProbe] = Field(default=None, alias="tcpSocket")
    # Optional runtime-only timeout override (seconds). K8s does not have this;
    # we use it to bound preStop execution when present.
    timeout_seconds: Optional[int] = Field(default=None, alias="timeoutSeconds")

    model_config = {"populate_by_name": True}


class LifecycleSpec(BaseModel):
    """Container lifecycle hooks."""

    post_start: Optional[LifecycleHandler] = Field(default=None, alias="postStart")
    pre_stop: Optional[LifecycleHandler] = Field(default=None, alias="preStop")

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
    # Optional Startup probe; when set, exporter emits startupProbe and
    # runtime should gate liveness checks until startup succeeds.
    startup: Optional[ProbeSpec] = None


class IngressSpec(BaseModel):
    """Ingress configuration targeting Caddy/nginx."""

    host: str
    path: str = Field(default="/")
    tls: bool = Field(default=True)
    # Phase 7: optional multi-paths and TLS secret passthrough
    paths: List[str] = Field(default_factory=list)
    tls_secret_name: Optional[str] = Field(default=None, alias="tlsSecretName")
    annotations: dict | None = None
    # Optional BYO TLS for Caddy writer (host cert/key paths)
    tls_cert_path: Optional[str] = Field(default=None, alias="tlsCertPath")
    tls_key_path: Optional[str] = Field(default=None, alias="tlsKeyPath")

    model_config = {"populate_by_name": True}


class ServiceSpec(BaseModel):
    """Service abstraction (single-host) providing a stable published port.

    Note: In this initial implementation, `service.port` is only supported when
    replicas == 1. For multi-replica services, the controller will fall back to
    per-replica ephemeral host ports and ingress load-balances to one endpoint.
    """

    class ServicePort(BaseModel):
        """Optional explicit Service port mapping for K8s export.

        - name: logical name (e.g., "http", "metrics")
        - port: Service port number (ClusterIP port)
        - targetPort: container port to target; defaults to matching containerPort by
          name/number when omitted.
        - protocol: defaults to TCP
        """

        name: str
        port: int
        target_port: Optional[int] = Field(default=None, alias="targetPort")
        protocol: str = Field(default="TCP")
        node_port: Optional[int] = Field(default=None, alias="nodePort")

        model_config = {"populate_by_name": True}

    # Optional service type for K8s exports
    type: Optional[Literal["ClusterIP", "NodePort", "LoadBalancer"]] = None
    external_traffic_policy: Optional[Literal["Cluster", "Local"]] = Field(
        default=None, alias="externalTrafficPolicy"
    )

    # Back-compat single-port fields (used by local runtime stable host port and
    # as defaults for exporter when ports[] is not provided)
    port: Optional[int] = Field(default=None, description="Host/Service port (single)")
    target_port: Optional[int] = Field(
        default=None,
        alias="targetPort",
        description="Container port to expose; defaults to first port",
    )
    # Optional multi-port mapping for exporter
    ports: List[ServicePort] = Field(default_factory=list)
    # Optional externalIPs for ClusterIP/NodePort Services
    external_ips: List[str] = Field(default_factory=list, alias="externalIPs")

    # Optional session affinity (K8s pass-through)
    session_affinity: Optional[Literal["None", "ClientIP"]] = Field(
        default=None, alias="sessionAffinity"
    )

    class SessionAffinityClientIP(BaseModel):
        timeout_seconds: Optional[int] = Field(default=None, alias="timeoutSeconds")

        model_config = {"populate_by_name": True}

    class SessionAffinityConfig(BaseModel):
        client_ip: Optional[SessionAffinityClientIP] = Field(default=None, alias="clientIP")

        model_config = {"populate_by_name": True}

    session_affinity_config: Optional[SessionAffinityConfig] = Field(
        default=None, alias="sessionAffinityConfig"
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

        # Accept common Kubernetes forms: 128Mi, 256M, 1Gi, 2G, 500Ki, and with optional trailing 'B'
        pattern = re.compile(r"^\d+(?:\.\d+)?\s*(?:[KMG](?:i)?(?:B)?|[kKmMgG])?$")
        if not pattern.match(s):
            raise ValueError("memory must be a number optionally suffixed by K/M/G or KiB/MiB/GiB")
        return s

    model_config = {"extra": "allow"}

    def quantity_map(self) -> dict[str, object]:
        """Return the quantity payload including extended resource keys."""
        data = self.model_dump(exclude_none=True)
        return {str(key): value for key, value in data.items() if value is not None}


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
    # Optional seccomp profile (maps to securityContext.seccompProfile)
    seccomp_type: Optional[Literal["RuntimeDefault", "Localhost", "Unconfined"]] = Field(
        default=None, alias="seccompProfileType"
    )
    seccomp_localhost_profile: Optional[str] = Field(default=None, alias="seccompLocalhostProfile")
    # Optional AppArmor profile annotation value: runtime/default | unconfined | localhost/<profile>
    apparmor_profile: Optional[str] = Field(default=None, alias="apparmorProfile")

    model_config = {"populate_by_name": True}


class PodSecuritySpec(BaseModel):
    """Pod-level security context subset.

    - fsGroup: numeric GID applied to mounted volumes
    - seccompProfile at Pod level (type + localhostProfile)
    - seLinuxOptions fields for SELinux context
    """

    fs_group: Optional[int] = Field(default=None, alias="fsGroup")
    seccomp_type: Optional[Literal["RuntimeDefault", "Localhost", "Unconfined"]] = Field(
        default=None, alias="seccompProfileType"
    )
    seccomp_localhost_profile: Optional[str] = Field(default=None, alias="seccompLocalhostProfile")
    selinux_user: Optional[str] = Field(default=None, alias="seLinuxUser")
    selinux_role: Optional[str] = Field(default=None, alias="seLinuxRole")
    selinux_type: Optional[str] = Field(default=None, alias="seLinuxType")
    selinux_level: Optional[str] = Field(default=None, alias="seLinuxLevel")

    model_config = {"populate_by_name": True}


class DNSConfigOption(BaseModel):
    name: str
    value: Optional[str] = None


class DNSConfig(BaseModel):
    nameservers: List[str] = Field(default_factory=list)
    searches: List[str] = Field(default_factory=list)
    options: List[DNSConfigOption] = Field(default_factory=list)


class HostAlias(BaseModel):
    ip: str
    hostnames: List[str] = Field(default_factory=list)


class VolumeSpec(BaseModel):
    """HostPath volume mapping."""

    host_path: str = Field(alias="hostPath")
    mount_path: str = Field(alias="mountPath")
    read_only: bool = Field(default=False, alias="readOnly")

    model_config = {"populate_by_name": True}


class VolumeDeviceSpec(BaseModel):
    """Raw device mapping (block volumes)."""

    host_path: str = Field(alias="hostPath")
    device_path: str = Field(alias="devicePath")
    read_only: bool = Field(default=False, alias="readOnly")

    model_config = {"populate_by_name": True}


class PvcMountSpec(BaseModel):
    """PVC-backed volume mount request (resolved via NetFS)."""

    claim_name: str = Field(alias="claimName")
    mount_path: str = Field(alias="mountPath")
    read_only: bool = Field(default=False, alias="readOnly")
    device_path: Optional[str] = Field(default=None, alias="devicePath")
    sub_path: Optional[str] = Field(default=None, alias="subPath")
    claim_template: bool = Field(default=False, alias="claimTemplate")
    namespace: Optional[str] = None

    model_config = {"populate_by_name": True}


class StorageRetention(str):
    Retain = "Retain"
    Delete = "Delete"


class StorageSpec(BaseModel):
    """Named persistent storage volume (PV-lite).

    The controller creates a Docker named volume per entry and mounts it at
    the specified path. Retention controls removal on app deletion.

    Optional fields (class/accessModes/volumeMode/readOnly) are reserved for
    NetFS-backed PVC mapping and future runtime integrations.
    """

    name: str
    mount_path: str = Field(alias="mountPath")
    retention: str = Field(default=StorageRetention.Retain)
    size: str | None = None  # reserved for future use
    storage_class: str | None = Field(default=None, alias="class")
    access_modes: list[str] | None = Field(default=None, alias="accessModes")
    volume_mode: str | None = Field(default=None, alias="volumeMode")
    read_only: bool = Field(default=False, alias="readOnly")

    model_config = {"populate_by_name": True}


class EmptyDirSpec(BaseModel):
    """Ephemeral emptyDir volume mount.

    - medium: "" (node filesystem) or "Memory" (tmpfs). Other K8s mediums are passed through if provided.
    - sizeLimit: optional Kubernetes quantity string (e.g., "256Mi").
    """

    name: str
    mount_path: str = Field(alias="mountPath")
    medium: Optional[str] = None
    size_limit: Optional[str] = Field(default=None, alias="sizeLimit")

    model_config = {"populate_by_name": True}


class ExportHints(BaseModel):
    """Optional exporter hints to suppress certain checks or toggle emissions.

    - emitPDB: request exporter to emit a PodDisruptionBudget when replicas>1.
    """

    emit_pdb: bool = Field(default=False, alias="emitPDB")
    # Suppress informational check about image multi-arch when you know your image is multi-arch
    suppress_image_multi_arch_warning: bool = Field(
        default=False, alias="suppressImageMultiArchWarning"
    )

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
    # Optional envFrom behavior: when true, exporter may emit envFrom for this secret
    env_from: bool = Field(default=False, alias="envFrom")


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
    # Optional envFrom behavior: when true, exporter may emit envFrom for this configmap
    env_from: bool = Field(default=False, alias="envFrom")

    model_config = {"populate_by_name": True}


class AppSpec(BaseModel):
    """Workload specification."""

    image: str
    workload: Literal["service", "job"] = Field(default="service")
    job_backoff_limit: Optional[int] = Field(default=None, alias="jobBackoffLimit")
    job_ttl_seconds_after_finished: Optional[int] = Field(
        default=None, alias="jobTtlSecondsAfterFinished"
    )
    command: Optional[List[str]] = None
    args: Optional[List[str]] = None
    env: List[dict[str, str]] = Field(default_factory=list)
    replicas: int = Field(default=1, ge=1)
    ports: List[PortSpec] = Field(default_factory=list)
    health: Optional[HealthSpec] = None
    lifecycle: Optional[LifecycleSpec] = None

    # Multi-container (exporter-level support; runtime runs a single container)
    class ContainerSpec(BaseModel):
        name: str
        image: str
        command: Optional[List[str]] = None
        args: Optional[List[str]] = None
        env: List[dict[str, str]] = Field(default_factory=list)
        ports: List[PortSpec] = Field(default_factory=list)
        resources: Optional[ResourcesSpec] = None
        security: Optional[SecuritySpec] = None
        working_dir: Optional[str] = Field(default=None, alias="workingDir")
        # Optional per-container probes
        health: Optional[HealthSpec] = None
        # Optional timeout for init containers (seconds). Ignored for main containers.
        timeout_seconds: Optional[int] = Field(default=None, alias="timeoutSeconds")

        # Optional additional mounts from the app's projection root (state/projections/...)
        # Each entry binds a subpath under /var/run/ae/config/<app> into a custom mountPath
        # inside this container. Useful to expose selected config/secret files at bespoke paths.
        class ProjectionMount(BaseModel):
            path: (
                str  # relative to /var/run/ae/config/<app> (e.g., "config/db", "secret/creds.json")
            )
            mount_path: str = Field(alias="mountPath")
            read_only: bool = Field(default=True, alias="readOnly")

            model_config = {"populate_by_name": True}

        projection_mounts: List[ProjectionMount] = Field(
            default_factory=list, alias="projectionMounts"
        )

        model_config = {"populate_by_name": True}

    containers: List[ContainerSpec] = Field(default_factory=list)
    init_containers: List[ContainerSpec] = Field(default_factory=list, alias="initContainers")
    ingress: Optional[IngressSpec] = None
    service: Optional[ServiceSpec] = None
    working_dir: Optional[str] = Field(default=None, alias="workingDir")
    termination_message_path: Optional[str] = Field(default=None, alias="terminationMessagePath")
    termination_message_policy: Optional[str] = Field(
        default=None, alias="terminationMessagePolicy"
    )
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
    volume_devices: List[VolumeDeviceSpec] = Field(default_factory=list, alias="volumeDevices")
    pvc_mounts: List[PvcMountSpec] = Field(default_factory=list, alias="pvcMounts")
    storage: List[StorageSpec] = Field(default_factory=list)
    empty_dirs: List[EmptyDirSpec] = Field(default_factory=list, alias="emptyDirs")
    # Optional exporter hints (purely affects export/check tooling)
    export_hints: Optional[ExportHints] = Field(default=None, alias="exportHints")
    # Image pull controls (pass-through to K8s export)
    image_pull_policy: Optional[Literal["Always", "IfNotPresent", "Never"]] = Field(
        default=None, alias="imagePullPolicy"
    )
    image_pull_secrets: List[str] = Field(default_factory=list, alias="imagePullSecrets")
    service_account_name: Optional[str] = Field(default=None, alias="serviceAccountName")
    runtime_class_name: Optional[str] = Field(default=None, alias="runtimeClassName")
    # Scheduling (pass-through for K8s export)
    affinity: dict | None = None
    tolerations: List[dict] = Field(default_factory=list)
    topology_spread_constraints: List[dict] = Field(
        default_factory=list, alias="topologySpreadConstraints"
    )
    priority_class_name: Optional[str] = Field(default=None, alias="priorityClassName")
    # Network policy (K8s export)
    network_policy: Optional[dict] = Field(default=None, alias="networkPolicy")
    # Pod-level security context
    pod_security: Optional[PodSecuritySpec] = Field(default=None, alias="podSecurity")
    # DNS policy/config (K8s export pass-through)
    dns_policy: Optional[Literal["Default", "ClusterFirst", "ClusterFirstWithHostNet", "None"]] = (
        Field(default=None, alias="dnsPolicy")
    )
    dns_config: Optional[DNSConfig] = Field(default=None, alias="dnsConfig")
    # Pod identity (K8s export pass-through)
    hostname: Optional[str] = None
    subdomain: Optional[str] = None
    # Host aliases (K8s export pass-through)
    host_aliases: List[HostAlias] = Field(default_factory=list, alias="hostAliases")
    # Other small pass-throughs
    enable_service_links: Optional[bool] = Field(default=None, alias="enableServiceLinks")
    share_process_namespace: Optional[bool] = Field(default=None, alias="shareProcessNamespace")
    host_network: Optional[bool] = Field(default=None, alias="hostNetwork")
    node_selector: dict[str, str] = Field(default_factory=dict, alias="nodeSelector")
    set_hostname_as_fqdn: Optional[bool] = Field(default=None, alias="setHostnameAsFQDN")
    host_pid: Optional[bool] = Field(default=None, alias="hostPID")
    host_ipc: Optional[bool] = Field(default=None, alias="hostIPC")

    model_config = {"populate_by_name": True}


class AppManifest(BaseModel):
    """Top-level workload manifest (Deployment)."""

    api_version: Literal["ae.dev/v1alpha1"] = Field(alias="apiVersion")
    kind: Literal["Deployment"]
    metadata: Metadata
    spec: AppSpec

    model_config = {"populate_by_name": True}

    @field_validator("kind", mode="before")
    @classmethod
    def _normalize_kind(cls, v: str):  # noqa: D401 - simple guard
        if isinstance(v, str):
            raw = v.strip()
            low = raw.lower()
            if low == "app":
                raise ValueError("kind 'App' is no longer supported; use kind 'Deployment'")
            if low == "deployment":
                return "Deployment"
        return v


class InferenceModelRef(BaseModel):
    """Model reference for an inference cell."""

    model_id: str = Field(alias="modelId")
    revision: str | None = None
    local_path: str = Field(alias="localPath")

    model_config = {"populate_by_name": True}


class InferenceParallelismSpec(BaseModel):
    """Parallelism settings."""

    tp: int = Field(default=1, ge=1)
    pp: int = Field(default=1, ge=1)


class InferenceRayPorts(BaseModel):
    """Pinned Ray port profile."""

    head_port: int = Field(default=6379, alias="headPort")
    node_manager_port: int = Field(default=10301, alias="nodeManagerPort")
    object_manager_port: int = Field(default=12345, alias="objectManagerPort")
    runtime_env_agent_port: int = Field(default=17001, alias="runtimeEnvAgentPort")
    min_worker_port: int = Field(default=20001, alias="minWorkerPort")
    max_worker_port: int = Field(default=20100, alias="maxWorkerPort")
    include_dashboard: bool = Field(default=False, alias="includeDashboard")
    dashboard_host: str = Field(default="127.0.0.1", alias="dashboardHost")
    dashboard_port: int = Field(default=8265, alias="dashboardPort")
    ray_client_server_port: int | None = Field(default=None, alias="rayClientServerPort")
    metrics_export_port: int | None = Field(default=None, alias="metricsExportPort")

    model_config = {"populate_by_name": True}


class InferenceExecutorSpec(BaseModel):
    """Executor selection and options."""

    type: Literal["ray", "mp"] = "ray"
    fallback_mode: Literal["none", "mp_on_failure"] = Field(
        default="mp_on_failure", alias="fallbackMode"
    )
    ray_scope: Literal["per-site", "per-cell", "shared"] = Field(
        default="per-site", alias="rayScope"
    )
    ray_auth_token_ref: str | None = Field(default=None, alias="rayAuthTokenRef")
    ray_ports: InferenceRayPorts = Field(default_factory=InferenceRayPorts, alias="rayPorts")
    ray_image: str = Field(default="rayproject/ray:latest", alias="rayImage")
    mp_image: str = Field(default="vllm/vllm-openai:latest", alias="mpImage")
    launcher_image: str = Field(default="python:3.12-slim", alias="launcherImage")
    dtype: str | None = None
    runtime_class_name: str | None = Field(default=None, alias="runtimeClassName")

    @field_validator("dtype", mode="before")
    @classmethod
    def _normalize_dtype(cls, v: Optional[str]):  # noqa: D401 - simple guard
        if v is None:
            return v
        raw = str(v).strip()
        return raw or None

    model_config = {"populate_by_name": True}


class InferenceMemberBootAssuranceSpec(BaseModel):
    """Local simulated boot assurance state for a candidate inference member."""

    status: Literal["verified", "unverified", "failed", "tampered"] = "verified"
    schedulable: bool = True
    quarantined: bool = False
    failure_reasons: List[str] = Field(default_factory=list, alias="failureReasons")
    alert: Literal["none", "pending", "emitted"] = "none"
    evidence_nonce: str | None = Field(default=None, alias="evidenceNonce")

    model_config = {"populate_by_name": True}

    @field_validator("failure_reasons", mode="before")
    @classmethod
    def _normalize_failure_reasons(cls, v):  # noqa: D401 - simple guard
        values = list(v or [])
        reasons = [str(item or "").strip() for item in values]
        if any(not item for item in reasons):
            raise ValueError("member bootAssurance.failureReasons must not contain blank reasons")
        return reasons

    @field_validator("evidence_nonce", mode="before")
    @classmethod
    def _normalize_nonce(cls, v: str | None) -> str | None:
        if v is None:
            return None
        text = str(v or "").strip()
        return text or None

    @model_validator(mode="after")
    def _validate_assurance_state(self) -> "InferenceMemberBootAssuranceSpec":
        if self.status == "verified":
            if self.quarantined:
                raise ValueError("verified member bootAssurance must not be quarantined")
            if not self.schedulable:
                raise ValueError("verified member bootAssurance must be schedulable")
            if self.failure_reasons:
                raise ValueError("verified member bootAssurance must not include failureReasons")
        else:
            if not self.quarantined:
                raise ValueError("failed member bootAssurance must be quarantined")
            if self.schedulable:
                raise ValueError("failed member bootAssurance must not be schedulable")
            if not self.failure_reasons:
                raise ValueError("failed member bootAssurance must include failureReasons")
            if self.alert == "none":
                raise ValueError("failed member bootAssurance must set alert pending or emitted")
        return self


class InferenceMemberSpec(BaseModel):
    """Candidate node for stage placement."""

    site_id: str = Field(alias="siteId")
    node_id: str = Field(alias="nodeId")
    gpu_count: int = Field(alias="gpuCount", ge=1)
    role: Literal["gateway", "cell-node"] | None = None
    compute_eligible: bool = Field(default=True, alias="computeEligible")
    boot_assurance: InferenceMemberBootAssuranceSpec | None = Field(
        default=None, alias="bootAssurance"
    )

    model_config = {"populate_by_name": True}


class InferenceCellAutonomySpec(BaseModel):
    """Disconnected operation intent for an inference cell contract."""

    connected_mode: Literal["normal-connected"] = Field(
        default="normal-connected", alias="connectedMode"
    )
    core_link_unavailable_mode: Literal["degraded-local-only"] = Field(
        default="degraded-local-only", alias="coreLinkUnavailableMode"
    )
    reconnect_mode: Literal["reconcile-on-restore"] = Field(
        default="reconcile-on-restore", alias="reconnectMode"
    )
    core_link_uptime_threshold_pct: float = Field(
        default=80.0, alias="coreLinkUptimeThresholdPct", ge=0.0, le=80.0
    )

    model_config = {"populate_by_name": True}


class InferenceGatewayDiscoverySpec(BaseModel):
    """LAN-local gateway discovery intent for an AI Max edge-cell fabric."""

    mode: Literal["lan-local"] = "lan-local"
    fabric_cell_count: int = Field(default=1, alias="fabricCellCount")
    lan_scope: str = Field(default="default-lan", alias="lanScope")
    gateway_peer_ids: List[str] = Field(default_factory=list, alias="gatewayPeerIds")

    model_config = {"populate_by_name": True}

    @model_validator(mode="after")
    def _validate_gateway_discovery(self) -> "InferenceGatewayDiscoverySpec":
        if self.fabric_cell_count not in {1, 2, 4, 8}:
            raise ValueError("gatewayDiscovery.fabricCellCount must be one of 1, 2, 4, or 8")
        scope = str(self.lan_scope or "").strip()
        if not scope:
            raise ValueError("gatewayDiscovery.lanScope must be non-empty")
        peers = [str(peer or "").strip() for peer in self.gateway_peer_ids]
        if any(not peer for peer in peers):
            raise ValueError("gatewayDiscovery.gatewayPeerIds must not contain blank ids")
        if len(set(peers)) != len(peers):
            raise ValueError("gatewayDiscovery.gatewayPeerIds must be unique")
        expected_peer_count = self.fabric_cell_count - 1
        if len(peers) != expected_peer_count:
            raise ValueError(
                "gatewayDiscovery.gatewayPeerIds must contain exactly "
                f"{expected_peer_count} peer id(s) for fabricCellCount={self.fabric_cell_count}"
            )
        self.lan_scope = scope
        self.gateway_peer_ids = peers
        return self


class InferenceInstallerAssuranceSpec(BaseModel):
    """Boot/system assurance intent for the AI Max NixOS installer."""

    secure_image_validation: Literal["enabled"] = Field(
        default="enabled", alias="secureImageValidation"
    )
    boot_validation: Literal["measured-verified"] = Field(
        default="measured-verified", alias="bootValidation"
    )
    tamper_detection: Literal["enabled"] = Field(default="enabled", alias="tamperDetection")
    validation_failure_action: Literal["disable-quarantine"] = Field(
        default="disable-quarantine", alias="validationFailureAction"
    )
    core_alerting: Literal["when-connected"] = Field(default="when-connected", alias="coreAlerting")

    model_config = {"populate_by_name": True}


class InferenceInstallerPostInstallSpec(BaseModel):
    """Post-install posture for a specific AI Max installer path."""

    auto_boot: Literal["enabled"] = Field(default="enabled", alias="autoBoot")
    connect_target: Literal["core", "gateway"] = Field(alias="connectTarget")
    usb_device_policy: Literal["disabled", "limited", "signed-only"] = Field(
        alias="usbDevicePolicy"
    )
    display_mode: Literal["telemetry", "connect-monitor-to-gateway"] = Field(alias="displayMode")

    model_config = {"populate_by_name": True}


class InferenceInstallerInstallPathSpec(BaseModel):
    """Role-specific install path for the single AI Max NixOS installer image."""

    path: Literal["gateway", "cell-node"]
    post_install: InferenceInstallerPostInstallSpec = Field(alias="postInstall")

    model_config = {"populate_by_name": True}

    @model_validator(mode="after")
    def _validate_post_install_posture(self) -> "InferenceInstallerInstallPathSpec":
        if self.path == "gateway":
            if self.post_install.connect_target != "core":
                raise ValueError("installer gateway path must connectTarget=core")
            if self.post_install.display_mode != "telemetry":
                raise ValueError("installer gateway path must displayMode=telemetry")
        elif self.path == "cell-node":
            if self.post_install.connect_target != "gateway":
                raise ValueError("installer cell-node path must connectTarget=gateway")
            if self.post_install.display_mode != "connect-monitor-to-gateway":
                raise ValueError(
                    "installer cell-node path must displayMode=connect-monitor-to-gateway"
                )
        return self


def _default_ai_max_installer_paths() -> list[InferenceInstallerInstallPathSpec]:
    return [
        InferenceInstallerInstallPathSpec(
            path="gateway",
            postInstall={
                "autoBoot": "enabled",
                "connectTarget": "core",
                "usbDevicePolicy": "signed-only",
                "displayMode": "telemetry",
            },
        ),
        InferenceInstallerInstallPathSpec(
            path="cell-node",
            postInstall={
                "autoBoot": "enabled",
                "connectTarget": "gateway",
                "usbDevicePolicy": "signed-only",
                "displayMode": "connect-monitor-to-gateway",
            },
        ),
    ]


class InferenceInstallerArtifactProvenanceSpec(BaseModel):
    """Local build provenance for the AI Max installer artifact scaffold."""

    builder: str = "k1s-public-stage7-local-simulator"
    source_revision: str = Field(default="public-dev-stage7", alias="sourceRevision")
    created_at: str = Field(default="2026-06-25T00:00:00Z", alias="createdAt")

    model_config = {"populate_by_name": True}

    @field_validator("builder", "source_revision", "created_at", mode="before")
    @classmethod
    def _require_non_blank(cls, v: str) -> str:
        text = str(v or "").strip()
        if not text:
            raise ValueError("installer artifact provenance fields must be non-empty")
        return text


class InferenceInstallerArtifactSpec(BaseModel):
    """Signed artifact manifest for the local AI Max installer scaffold."""

    name: Literal["nixos-ai-max-edge-cell-installer"] = "nixos-ai-max-edge-cell-installer"
    profile: Literal["nixos-ai-max-edge-cell-installer-v1"] = "nixos-ai-max-edge-cell-installer-v1"
    image: Literal["nixos-ai-max-edge-cell-installer"] = "nixos-ai-max-edge-cell-installer"
    version: str = "stage7-local"
    artifact_digest: str = Field(
        default=_AI_MAX_INSTALLER_ARTIFACT_DIGEST,
        alias="artifactDigest",
    )
    manifest_digest: str = Field(
        default=_AI_MAX_INSTALLER_MANIFEST_DIGEST,
        alias="manifestDigest",
    )
    path_coverage: List[Literal["gateway", "cell-node"]] = Field(
        default_factory=lambda: ["gateway", "cell-node"], alias="pathCoverage"
    )
    provenance: InferenceInstallerArtifactProvenanceSpec = Field(
        default_factory=InferenceInstallerArtifactProvenanceSpec
    )

    model_config = {"populate_by_name": True}

    @field_validator("version", mode="before")
    @classmethod
    def _require_version(cls, v: str) -> str:
        text = str(v or "").strip()
        if not text:
            raise ValueError("installer artifact version must be non-empty")
        return text

    @field_validator("artifact_digest", "manifest_digest", mode="before")
    @classmethod
    def _validate_digest(cls, v: str) -> str:
        text = str(v or "").strip().lower()
        if not _SHA256_DIGEST_RE.fullmatch(text):
            raise ValueError("installer artifact digests must be sha256:<64 hex>")
        return text


class InferenceInstallerSignatureEnvelopeSpec(BaseModel):
    """Local signature envelope for the AI Max installer artifact scaffold."""

    algorithm: Literal["k1s-local-sim-ed25519-sha256"] = "k1s-local-sim-ed25519-sha256"
    signing_key_id: Literal["k1s-core-root-of-trust"] = Field(
        default="k1s-core-root-of-trust", alias="signingKeyId"
    )
    signed_digest: str = Field(
        default=_AI_MAX_INSTALLER_MANIFEST_DIGEST,
        alias="signedDigest",
    )
    signature: str = _AI_MAX_INSTALLER_SIGNATURE

    model_config = {"populate_by_name": True}

    @field_validator("signed_digest", mode="before")
    @classmethod
    def _validate_signed_digest(cls, v: str) -> str:
        text = str(v or "").strip().lower()
        if not _SHA256_DIGEST_RE.fullmatch(text):
            raise ValueError("installer signature signedDigest must be sha256:<64 hex>")
        return text

    @field_validator("signature", mode="before")
    @classmethod
    def _validate_signature(cls, v: str) -> str:
        text = str(v or "").strip()
        if not text:
            raise ValueError("installer signature must be non-empty")
        if not text.startswith("k1s-sim-signature:"):
            raise ValueError("installer signature must use the k1s local simulation envelope")
        return text


class InferenceInstallerRoleScaffoldSpec(BaseModel):
    """Role-specific installed-system scaffold emitted by the single installer image."""

    role: Literal["gateway", "cell-node"]
    module_ref: str = Field(alias="moduleRef")
    config_ref: str = Field(alias="configRef")
    derived_from_manifest_digest: str = Field(
        default=_AI_MAX_INSTALLER_MANIFEST_DIGEST, alias="derivedFromManifestDigest"
    )
    post_install: InferenceInstallerPostInstallSpec = Field(alias="postInstall")

    model_config = {"populate_by_name": True}

    @field_validator("module_ref", "config_ref", mode="before")
    @classmethod
    def _require_ref(cls, v: str) -> str:
        text = str(v or "").strip()
        if not text:
            raise ValueError("installer role scaffold moduleRef and configRef must be non-empty")
        return text

    @field_validator("derived_from_manifest_digest", mode="before")
    @classmethod
    def _validate_derived_digest(cls, v: str) -> str:
        text = str(v or "").strip().lower()
        if not _SHA256_DIGEST_RE.fullmatch(text):
            raise ValueError(
                "installer role scaffold derivedFromManifestDigest must be sha256:<64 hex>"
            )
        return text

    @model_validator(mode="after")
    def _validate_role_posture(self) -> "InferenceInstallerRoleScaffoldSpec":
        if self.role == "gateway":
            if self.post_install.connect_target != "core":
                raise ValueError("installer gateway role scaffold must connectTarget=core")
            if self.post_install.display_mode != "telemetry":
                raise ValueError("installer gateway role scaffold must displayMode=telemetry")
        elif self.role == "cell-node":
            if self.post_install.connect_target != "gateway":
                raise ValueError("installer cell-node role scaffold must connectTarget=gateway")
            if self.post_install.display_mode != "connect-monitor-to-gateway":
                raise ValueError(
                    "installer cell-node role scaffold must displayMode=connect-monitor-to-gateway"
                )
        return self


def _default_ai_max_installer_role_scaffolds() -> list[InferenceInstallerRoleScaffoldSpec]:
    return [
        InferenceInstallerRoleScaffoldSpec(
            role="gateway",
            moduleRef="nixos/modules/ai-max/installer/gateway.nix",
            configRef="nixos/configs/ai-max/gateway-installed-system.nix",
            derivedFromManifestDigest=_AI_MAX_INSTALLER_MANIFEST_DIGEST,
            postInstall={
                "autoBoot": "enabled",
                "connectTarget": "core",
                "usbDevicePolicy": "signed-only",
                "displayMode": "telemetry",
            },
        ),
        InferenceInstallerRoleScaffoldSpec(
            role="cell-node",
            moduleRef="nixos/modules/ai-max/installer/cell-node.nix",
            configRef="nixos/configs/ai-max/cell-node-installed-system.nix",
            derivedFromManifestDigest=_AI_MAX_INSTALLER_MANIFEST_DIGEST,
            postInstall={
                "autoBoot": "enabled",
                "connectTarget": "gateway",
                "usbDevicePolicy": "limited",
                "displayMode": "connect-monitor-to-gateway",
            },
        ),
    ]


class InferenceInstallerBootEvidenceVerificationSpec(BaseModel):
    """Local verification result for simulated AI Max boot evidence."""

    status: Literal["verified"] = "verified"
    verifier: Literal["k1s-local-boot-evidence-verifier-v1"] = "k1s-local-boot-evidence-verifier-v1"
    trust_root: Literal["k1s-core-root-of-trust"] = Field(
        default="k1s-core-root-of-trust", alias="trustRoot"
    )
    failure_reasons: List[str] = Field(default_factory=list, alias="failureReasons")

    model_config = {"populate_by_name": True}

    @model_validator(mode="after")
    def _validate_verification_result(self) -> "InferenceInstallerBootEvidenceVerificationSpec":
        if self.failure_reasons:
            raise ValueError("installer boot evidence failureReasons must be empty when verified")
        return self


class InferenceInstallerBootEvidenceSpec(BaseModel):
    """Deterministic simulator evidence for installed-system boot posture."""

    node_id: str = Field(alias="nodeId")
    role: Literal["gateway", "cell-node"]
    installer_profile: Literal["nixos-ai-max-edge-cell-installer-v1"] = Field(
        default="nixos-ai-max-edge-cell-installer-v1", alias="installerProfile"
    )
    installer_image: Literal["nixos-ai-max-edge-cell-installer"] = Field(
        default="nixos-ai-max-edge-cell-installer", alias="installerImage"
    )
    artifact_digest: str = Field(default=_AI_MAX_INSTALLER_ARTIFACT_DIGEST, alias="artifactDigest")
    manifest_digest: str = Field(default=_AI_MAX_INSTALLER_MANIFEST_DIGEST, alias="manifestDigest")
    boot_measurement_digest: str = Field(alias="bootMeasurementDigest")
    signing_key_id: Literal["k1s-core-root-of-trust"] = Field(
        default="k1s-core-root-of-trust", alias="signingKeyId"
    )
    verifier_trust_root: Literal["k1s-core-root-of-trust"] = Field(
        default="k1s-core-root-of-trust", alias="verifierTrustRoot"
    )
    nonce: str
    created_at: str = Field(default=_AI_MAX_BOOT_EVIDENCE_CREATED_AT, alias="createdAt")
    verification: InferenceInstallerBootEvidenceVerificationSpec = Field(
        default_factory=InferenceInstallerBootEvidenceVerificationSpec
    )

    model_config = {"populate_by_name": True}

    @field_validator("node_id", "nonce", "created_at", mode="before")
    @classmethod
    def _require_non_blank(cls, v: str) -> str:
        text = str(v or "").strip()
        if not text:
            raise ValueError(
                "installer boot evidence nodeId, nonce, and createdAt must be non-empty"
            )
        return text

    @field_validator("artifact_digest", "manifest_digest", "boot_measurement_digest", mode="before")
    @classmethod
    def _validate_boot_digest(cls, v: str) -> str:
        text = str(v or "").strip().lower()
        if not _SHA256_DIGEST_RE.fullmatch(text):
            raise ValueError("installer boot evidence digests must be sha256:<64 hex>")
        return text

    @model_validator(mode="after")
    def _validate_simulated_nonce(self) -> "InferenceInstallerBootEvidenceSpec":
        expected_nonce = (
            _AI_MAX_GATEWAY_BOOT_NONCE if self.role == "gateway" else _AI_MAX_CELL_NODE_BOOT_NONCE
        )
        if self.nonce != expected_nonce:
            raise ValueError("installer boot evidence nonce is stale or does not match role")
        return self


def _default_ai_max_installer_boot_evidence() -> list[InferenceInstallerBootEvidenceSpec]:
    return [
        InferenceInstallerBootEvidenceSpec(
            nodeId="gateway-1",
            role="gateway",
            installerProfile="nixos-ai-max-edge-cell-installer-v1",
            installerImage="nixos-ai-max-edge-cell-installer",
            artifactDigest=_AI_MAX_INSTALLER_ARTIFACT_DIGEST,
            manifestDigest=_AI_MAX_INSTALLER_MANIFEST_DIGEST,
            bootMeasurementDigest=_AI_MAX_GATEWAY_BOOT_MEASUREMENT_DIGEST,
            signingKeyId="k1s-core-root-of-trust",
            verifierTrustRoot="k1s-core-root-of-trust",
            nonce=_AI_MAX_GATEWAY_BOOT_NONCE,
            createdAt=_AI_MAX_BOOT_EVIDENCE_CREATED_AT,
        ),
        InferenceInstallerBootEvidenceSpec(
            nodeId="cell-node-1",
            role="cell-node",
            installerProfile="nixos-ai-max-edge-cell-installer-v1",
            installerImage="nixos-ai-max-edge-cell-installer",
            artifactDigest=_AI_MAX_INSTALLER_ARTIFACT_DIGEST,
            manifestDigest=_AI_MAX_INSTALLER_MANIFEST_DIGEST,
            bootMeasurementDigest=_AI_MAX_CELL_NODE_BOOT_MEASUREMENT_DIGEST,
            signingKeyId="k1s-core-root-of-trust",
            verifierTrustRoot="k1s-core-root-of-trust",
            nonce=_AI_MAX_CELL_NODE_BOOT_NONCE,
            createdAt=_AI_MAX_BOOT_EVIDENCE_CREATED_AT,
        ),
    ]


class InferenceInstallerSpec(BaseModel):
    """Declarative NixOS installer contract for the AI Max edge-cell profile."""

    profile: Literal["nixos-ai-max-edge-cell-installer-v1"] = "nixos-ai-max-edge-cell-installer-v1"
    image: Literal["nixos-ai-max-edge-cell-installer"] = "nixos-ai-max-edge-cell-installer"
    signed_by: Literal["k1s-core-root-of-trust"] = Field(
        default="k1s-core-root-of-trust", alias="signedBy"
    )
    install_paths: List[InferenceInstallerInstallPathSpec] = Field(
        default_factory=_default_ai_max_installer_paths, alias="installPaths"
    )
    assurance: InferenceInstallerAssuranceSpec = Field(
        default_factory=InferenceInstallerAssuranceSpec
    )
    artifact: InferenceInstallerArtifactSpec = Field(default_factory=InferenceInstallerArtifactSpec)
    signature: InferenceInstallerSignatureEnvelopeSpec = Field(
        default_factory=InferenceInstallerSignatureEnvelopeSpec
    )
    role_scaffolds: List[InferenceInstallerRoleScaffoldSpec] = Field(
        default_factory=_default_ai_max_installer_role_scaffolds,
        alias="roleScaffolds",
    )
    boot_evidence: List[InferenceInstallerBootEvidenceSpec] = Field(
        default_factory=_default_ai_max_installer_boot_evidence,
        alias="bootEvidence",
    )

    model_config = {"populate_by_name": True}

    @model_validator(mode="after")
    def _validate_installer_contract(self) -> "InferenceInstallerSpec":
        paths = [item.path for item in self.install_paths]
        if len(paths) != 2 or set(paths) != _AI_MAX_INSTALLER_PATHS:
            raise ValueError("installer.installPaths must contain exactly gateway and cell-node")
        if len(set(paths)) != len(paths):
            raise ValueError("installer.installPaths must not contain duplicate paths")
        if set(self.artifact.path_coverage) != set(paths):
            raise ValueError("installer artifact pathCoverage must cover gateway and cell-node")
        if len(set(self.artifact.path_coverage)) != len(self.artifact.path_coverage):
            raise ValueError("installer artifact pathCoverage must not contain duplicate paths")
        if self.artifact.profile != self.profile:
            raise ValueError("installer artifact profile must match installer profile")
        if self.artifact.image != self.image:
            raise ValueError("installer artifact image must match installer image")
        if self.signature.signing_key_id != self.signed_by:
            raise ValueError("installer signature signingKeyId must match signedBy")
        if self.signature.signed_digest != self.artifact.manifest_digest:
            raise ValueError("installer signature signedDigest must match artifact manifestDigest")
        role_scaffold_roles = [item.role for item in self.role_scaffolds]
        if len(set(role_scaffold_roles)) != len(role_scaffold_roles):
            raise ValueError("installer.roleScaffolds must not contain duplicate roles")
        if len(role_scaffold_roles) != 2 or set(role_scaffold_roles) != _AI_MAX_INSTALLER_PATHS:
            raise ValueError("installer.roleScaffolds must contain exactly gateway and cell-node")
        for scaffold in self.role_scaffolds:
            if scaffold.derived_from_manifest_digest != self.artifact.manifest_digest:
                raise ValueError(
                    "installer role scaffold derivedFromManifestDigest must match "
                    "artifact manifestDigest"
                )
        evidence_roles = [item.role for item in self.boot_evidence]
        if len(set(evidence_roles)) != len(evidence_roles):
            raise ValueError("installer.bootEvidence must not contain duplicate roles")
        if len(evidence_roles) != 2 or set(evidence_roles) != _AI_MAX_INSTALLER_PATHS:
            raise ValueError("installer.bootEvidence must contain exactly gateway and cell-node")
        role_scaffold_set = set(role_scaffold_roles)
        for evidence in self.boot_evidence:
            if evidence.role not in role_scaffold_set:
                raise ValueError("installer boot evidence role must match installer role scaffold")
            if evidence.installer_profile != self.profile:
                raise ValueError("installer boot evidence profile must match installer profile")
            if evidence.installer_image != self.image:
                raise ValueError("installer boot evidence image must match installer image")
            if evidence.artifact_digest != self.artifact.artifact_digest:
                raise ValueError(
                    "installer boot evidence artifactDigest must match artifact digest"
                )
            if evidence.manifest_digest != self.artifact.manifest_digest:
                raise ValueError(
                    "installer boot evidence manifestDigest must match artifact manifestDigest"
                )
            if evidence.signing_key_id != self.signed_by:
                raise ValueError("installer boot evidence signingKeyId must match signedBy")
            if evidence.verifier_trust_root != self.signed_by:
                raise ValueError("installer boot evidence verifierTrustRoot must match signedBy")
            if evidence.verification.trust_root != self.signed_by:
                raise ValueError(
                    "installer boot evidence verification trustRoot must match signedBy"
                )
        self.install_paths = sorted(
            self.install_paths,
            key=lambda item: 0 if item.path == "gateway" else 1,
        )
        self.role_scaffolds = sorted(
            self.role_scaffolds,
            key=lambda item: 0 if item.role == "gateway" else 1,
        )
        self.boot_evidence = sorted(
            self.boot_evidence,
            key=lambda item: 0 if item.role == "gateway" else 1,
        )
        self.artifact.path_coverage = sorted(
            self.artifact.path_coverage,
            key=lambda item: 0 if item == "gateway" else 1,
        )
        return self


class InferenceCellContractSpec(BaseModel):
    """Public opt-in validation contract for known inference cell shapes."""

    profile: Literal["ai-max-edge-cell-v1"]
    gateway_reserved_gpu_fraction: float = Field(
        default=0.0, alias="gatewayReservedGpuFraction", ge=0.0, lt=1.0
    )
    autonomy: InferenceCellAutonomySpec = Field(default_factory=InferenceCellAutonomySpec)
    gateway_discovery: InferenceGatewayDiscoverySpec = Field(
        default_factory=InferenceGatewayDiscoverySpec, alias="gatewayDiscovery"
    )
    installer: InferenceInstallerSpec = Field(default_factory=InferenceInstallerSpec)

    model_config = {"populate_by_name": True}


class InferencePlacementPolicy(BaseModel):
    """Placement policy knobs."""

    pack_stages_by_site: bool = Field(default=True, alias="packStagesBySite")

    model_config = {"populate_by_name": True}


class InferenceFabricSpec(BaseModel):
    """Per-cell fabric policy."""

    provider: Literal["wg_ephemeral"] = "wg_ephemeral"
    mode: Literal["lan_direct", "wg_ephemeral"] = "lan_direct"
    policy_mode: Literal["strict_membership", "strict_ports"] = Field(
        default="strict_membership", alias="policyMode"
    )
    ttl_seconds: int = Field(default=300, alias="ttlSeconds", ge=30)

    model_config = {"populate_by_name": True}


class InferenceRendezvousSpec(BaseModel):
    """Port ranges and master stage."""

    master_stage: int = Field(default=0, alias="masterStage", ge=0)
    master_port_min: int = Field(default=22000, alias="masterPortMin")
    master_port_max: int = Field(default=22100, alias="masterPortMax")
    api_port_min: int = Field(default=18080, alias="apiPortMin")
    api_port_max: int = Field(default=18180, alias="apiPortMax")

    model_config = {"populate_by_name": True}


class InferenceHealthSpec(BaseModel):
    """Lifecycle deadlines and restart policy."""

    workers_deadline_s: int = Field(default=180, alias="workersDeadlineSeconds")
    leader_deadline_s: int = Field(default=180, alias="leaderDeadlineSeconds")
    join_deadline_s: int = Field(default=120, alias="joinDeadlineSeconds")
    max_restarts: int = Field(default=5, alias="maxRestarts", ge=0)
    backoff_initial_s: float = Field(default=3.0, alias="backoffInitialSeconds")
    backoff_multiplier: float = Field(default=2.0, alias="backoffMultiplier")
    backoff_max_s: float = Field(default=60.0, alias="backoffMaxSeconds")

    model_config = {"populate_by_name": True}


class InferenceLinkBudget(BaseModel):
    """Hard link caps for admission."""

    rtt_p95_ms_max: float = Field(default=30.0, alias="rttP95MsMax")
    jitter_p95_ms_max: float = Field(default=3.0, alias="jitterP95MsMax")
    loss_pct_max: float = Field(default=0.01, alias="lossPctMax")

    model_config = {"populate_by_name": True}


class InferencePerfBudget(BaseModel):
    """Relative latency budget for network overhead."""

    compute_token_ms_p50: float | None = Field(default=None, alias="computeTokenMsP50")
    alpha_net: float = Field(default=0.25, alias="alphaNet")
    beta_jitter: float = Field(default=0.05, alias="betaJitter")
    loss_pct_max: float = Field(default=0.01, alias="lossPctMax")

    model_config = {"populate_by_name": True}


class LinkMetricSample(BaseModel):
    """Observed site-to-site metric sample used for admission."""

    from_site: str = Field(alias="fromSite")
    to_site: str = Field(alias="toSite")
    rtt_p95_ms: float = Field(alias="rttP95Ms")
    jitter_p95_ms: float = Field(default=0.0, alias="jitterP95Ms")
    loss_pct: float = Field(default=0.0, alias="lossPct")

    model_config = {"populate_by_name": True}


class InferenceCellSpec(BaseModel):
    """Inference cell desired state."""

    cell_contract: InferenceCellContractSpec | None = Field(default=None, alias="cellContract")
    model: InferenceModelRef
    parallelism: InferenceParallelismSpec = Field(default_factory=InferenceParallelismSpec)
    executor: InferenceExecutorSpec = Field(default_factory=InferenceExecutorSpec)
    members: List[InferenceMemberSpec] = Field(default_factory=list)
    placement_policy: InferencePlacementPolicy = Field(
        default_factory=InferencePlacementPolicy, alias="placementPolicy"
    )
    fabric: InferenceFabricSpec = Field(default_factory=InferenceFabricSpec)
    rendezvous: InferenceRendezvousSpec = Field(default_factory=InferenceRendezvousSpec)
    health: InferenceHealthSpec = Field(default_factory=InferenceHealthSpec)
    link_budget: InferenceLinkBudget = Field(
        default_factory=InferenceLinkBudget, alias="linkBudget"
    )
    perf_budget: InferencePerfBudget = Field(
        default_factory=InferencePerfBudget, alias="perfBudget"
    )
    link_metrics: List[LinkMetricSample] = Field(default_factory=list, alias="linkMetrics")

    model_config = {"populate_by_name": True}

    @model_validator(mode="after")
    def _validate_cell_contract(self) -> "InferenceCellSpec":
        contract = self.cell_contract
        if contract is None:
            return self
        if contract.profile == "ai-max-edge-cell-v1":
            _validate_ai_max_edge_cell_contract(self.members)
        return self


def _validate_ai_max_edge_cell_contract(members: list[InferenceMemberSpec]) -> None:
    if len(members) != 4:
        raise ValueError("ai-max-edge-cell-v1 requires exactly 4 total members")

    roles = [member.role for member in members]
    gateway_count = roles.count("gateway")
    cell_node_count = roles.count("cell-node")
    if gateway_count != 1:
        raise ValueError("ai-max-edge-cell-v1 requires exactly 1 gateway member")
    if cell_node_count != 3:
        raise ValueError("ai-max-edge-cell-v1 requires exactly 3 cell-node members")

    ineligible = [member.node_id for member in members if not member.compute_eligible]
    if ineligible:
        nodes = ", ".join(ineligible)
        raise ValueError(
            f"ai-max-edge-cell-v1 requires all members to be compute eligible: {nodes}"
        )


class InferenceCellManifest(BaseModel):
    """Top-level inference cell manifest."""

    api_version: Literal["ae.dev/v1alpha1"] = Field(alias="apiVersion")
    kind: Literal["InferenceCell"]
    metadata: Metadata
    spec: InferenceCellSpec

    model_config = {"populate_by_name": True}

    @field_validator("kind", mode="before")
    @classmethod
    def _normalize_kind(cls, v: str):  # noqa: D401 - simple guard
        if isinstance(v, str) and v.strip().lower() == "inferencecell":
            return "InferenceCell"
        return v


class InferenceCellSetSpec(BaseModel):
    """Replica-set style template for inference cells."""

    replicas: int = Field(default=1, ge=0)
    template: InferenceCellSpec
    name_format: str = Field(default="{set}-{i:03d}", alias="nameFormat")

    model_config = {"populate_by_name": True}


class InferenceCellSetManifest(BaseModel):
    """Top-level inference cellset manifest."""

    api_version: Literal["ae.dev/v1alpha1"] = Field(alias="apiVersion")
    kind: Literal["InferenceCellSet"]
    metadata: Metadata
    spec: InferenceCellSetSpec

    model_config = {"populate_by_name": True}

    @field_validator("kind", mode="before")
    @classmethod
    def _normalize_kind(cls, v: str):  # noqa: D401 - simple guard
        if isinstance(v, str) and v.strip().lower() == "inferencecellset":
            return "InferenceCellSet"
        return v


ManifestDocument = AppManifest | InferenceCellManifest | InferenceCellSetManifest


def parse_manifest_document(data: dict, *, source: str = "manifest") -> ManifestDocument:
    """Parse any supported ae.dev/v1alpha1 manifest kind."""

    kind = str(data.get("kind") or "").strip().lower()
    try:
        if kind == "deployment":
            return AppManifest.model_validate(data)
        if kind == "inferencecell":
            return InferenceCellManifest.model_validate(data)
        if kind == "inferencecellset":
            return InferenceCellSetManifest.model_validate(data)
    except ValidationError as exc:
        raise ManifestError(f"{source} failed validation: {exc}") from exc
    raise ManifestError(
        f"{source} has unsupported kind {data.get('kind')!r}; expected Deployment, InferenceCell, or InferenceCellSet"
    )


def load_manifest(path: Path) -> AppManifest:
    """Load a Deployment manifest from YAML."""

    try:
        data = yaml.safe_load(path.read_text())
    except FileNotFoundError as exc:
        raise ManifestError(f"Manifest {path} not found") from exc
    except yaml.YAMLError as exc:
        raise ManifestError(f"Failed to parse YAML for manifest {path}: {exc}") from exc

    if not isinstance(data, dict):
        raise ManifestError(f"Manifest {path} must be a YAML mapping")

    manifest = parse_manifest_document(data, source=f"Manifest {path}")
    if not isinstance(manifest, AppManifest):
        raise ManifestError(f"Manifest {path} must have kind Deployment")
    return manifest


def load_any_manifest(path: Path) -> ManifestDocument:
    """Load any supported ae.dev/v1alpha1 manifest kind from YAML."""

    try:
        data = yaml.safe_load(path.read_text())
    except FileNotFoundError as exc:
        raise ManifestError(f"Manifest {path} not found") from exc
    except yaml.YAMLError as exc:
        raise ManifestError(f"Failed to parse YAML for manifest {path}: {exc}") from exc

    if not isinstance(data, dict):
        raise ManifestError(f"Manifest {path} must be a YAML mapping")
    return parse_manifest_document(data, source=f"Manifest {path}")


def normalize_namespace(namespace: str | None) -> str | None:
    if namespace is None:
        return None
    ns = str(namespace).strip()
    return ns or None


def app_key(name: str, namespace: str | None) -> str:
    ns = normalize_namespace(namespace)
    if not ns or ns == DEFAULT_NAMESPACE:
        return name
    return f"{ns}--{name}"


def split_app_key(app_name: str) -> tuple[str, str]:
    raw = str(app_name or "")
    if "--" in raw:
        ns, name = raw.split("--", 1)
        if ns:
            return ns, name
    return DEFAULT_NAMESPACE, raw


def format_app_ref(app_name: str) -> str:
    ns, name = split_app_key(app_name)
    return f"{ns}/{name}"


def parse_app_ref(ref: str) -> tuple[str | None, str]:
    raw = str(ref or "").strip()
    if "/" in raw:
        ns, name = raw.split("/", 1)
        return normalize_namespace(ns), name
    if "--" in raw:
        ns, name = raw.split("--", 1)
        return normalize_namespace(ns), name
    return None, raw


def app_key_for_manifest(manifest: "AppManifest") -> str:
    return app_key(manifest.metadata.name, getattr(manifest.metadata, "namespace", None))


def k8s_labels_for_manifest(manifest: "AppManifest") -> dict[str, str]:
    labels: dict[str, str] = {}
    try:
        labels.update({str(k): str(v) for k, v in (manifest.metadata.labels or {}).items()})
    except Exception:
        labels = {}
    labels.setdefault("app", manifest.metadata.name)
    labels.setdefault("app.kubernetes.io/name", manifest.metadata.name)
    labels.setdefault("app.kubernetes.io/instance", manifest.metadata.name)
    labels.setdefault("app.kubernetes.io/managed-by", "k1s")
    return labels


def runtime_labels_for_manifest(
    manifest: "AppManifest", *, app_name: str | None = None
) -> dict[str, str]:
    labels = k8s_labels_for_manifest(manifest)
    labels["ae.app"] = app_name or app_key_for_manifest(manifest)
    labels["ae.namespace"] = getattr(manifest.metadata, "namespace", None) or DEFAULT_NAMESPACE
    return labels


def all_pvc_mounts(manifest: "AppManifest") -> list[PvcMountSpec]:
    mounts: list[PvcMountSpec] = []
    mounts.extend(list(getattr(manifest.spec, "pvc_mounts", []) or []))
    for c in getattr(manifest.spec, "containers", []) or []:
        mounts.extend(list(getattr(c, "pvc_mounts", []) or []))
    for c in getattr(manifest.spec, "init_containers", []) or []:
        mounts.extend(list(getattr(c, "pvc_mounts", []) or []))
    return mounts


# ruff: noqa
