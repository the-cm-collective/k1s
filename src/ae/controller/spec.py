# ruff: noqa: E501,UP006,UP007,UP017
"""Declarative specification models for the ae application engine."""

from __future__ import annotations

from pathlib import Path
from typing import List, Literal, Optional

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator


DEFAULT_NAMESPACE = "default"


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


class InferenceMemberSpec(BaseModel):
    """Candidate node for stage placement."""

    site_id: str = Field(alias="siteId")
    node_id: str = Field(alias="nodeId")
    gpu_count: int = Field(alias="gpuCount", ge=1)

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
