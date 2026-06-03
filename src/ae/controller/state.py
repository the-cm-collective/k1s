# ruff: noqa: E501,UP006,UP007,UP017
"""State persistence helpers backed by SQLite (default) or Postgres (optional)."""

from __future__ import annotations

import json
import os
import sqlite3
import hashlib
import uuid
from dataclasses import InitVar, dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

try:  # Optional Postgres backend
    import psycopg
except Exception:  # pragma: no cover - optional dependency
    psycopg = None  # type: ignore

from ae.accelerators import link_metric_inventory, normalize_capabilities
from ae.controller.health import HealthReport
from ae.controller.spec import (
    DEFAULT_NAMESPACE,
    AppManifest,
    InferenceCellManifest,
    InferenceCellSetManifest,
    app_key,
    app_key_for_manifest,
)
from ae.fabric.locality import (
    FabricAdvisoryRequestRecord,
    FabricAdvisoryResponseRecord,
    FabricChunkRecord,
    FabricCognitiveSignalRecord,
    FabricDasCellBundleRecord,
    FabricDasQueryTraceRecord,
    FabricDasReplicationRecord,
    FabricDecisionTraceRecord,
    FabricLandingZoneRecord,
    FabricMovementRecord,
    FabricResidencyRecord,
    FabricTransferCapabilityRecord,
    FabricTransferLeaseRecord,
    FabricTransportAttemptRecord,
    advisory_request_from_payload,
    advisory_request_payload,
    advisory_response_from_payload,
    advisory_response_payload,
    chunk_record_from_payload,
    chunk_record_payload,
    cognitive_signal_from_payload,
    cognitive_signal_payload,
    das_cell_bundle_from_payload,
    das_cell_bundle_payload,
    das_query_trace_from_payload,
    das_query_trace_payload,
    das_replication_from_payload,
    das_replication_payload,
    decision_trace_from_payload,
    decision_trace_payload,
    landing_zone_from_payload,
    landing_zone_payload,
    movement_record_from_payload,
    movement_record_payload,
    normalize_chunk_id,
    residency_record_from_payload,
    residency_record_payload,
    transfer_capability_from_payload,
    transfer_capability_payload,
    transfer_lease_from_payload,
    transfer_lease_payload,
    transport_attempt_from_payload,
    transport_attempt_payload,
)
from ae.ha.fencing import parse_envelope, work_operation
from ae.resources import loader as resource_loader
from ae.runtime import RuntimeResult

_UNSET = object()


@dataclass(slots=True)
class AppStatus:
    """Latest reconcile snapshot for an application."""

    app_name: str
    desired_replicas: int
    ready_replicas: int
    live_replicas: int
    revision: int
    revision_status: str
    image: str
    created: int
    updated: int
    removed: int
    ingress_host: str | None = None
    ingress_path: str | None = None
    current_revision_ready_replicas: int = 0
    current_revision_live_replicas: int = 0
    old_revision_ready_replicas: int = 0
    old_revision_live_replicas: int = 0
    overlap_ready_replicas: int = 0
    overlap_live_replicas: int = 0


@dataclass(slots=True)
class RegistryEntry:
    """Registered desired-state manifest for reconciliation."""

    app_name: str
    manifest: AppManifest
    spec_hash: str
    source: str
    labels: dict
    updated_at: datetime
    resource_version: int = 0


@dataclass(slots=True)
class AuthorityObjectEntry:
    """Shared-authority shim object persisted outside the legacy apishim DB."""

    group: str
    version: str
    resource: str
    namespace: str | None
    name: str
    kind: str
    metadata: dict
    spec: dict
    status: dict
    updated_at: datetime
    resource_version: int = 0


@dataclass(slots=True)
class WorkloadMetricsSnapshot:
    """Aggregated workload metrics used by the HA HPA controller."""

    app_name: str
    controller_id: str
    controller_epoch: int
    collected_at: datetime
    cpu_utilization: float | None
    memory_utilization: float | None
    memory_bytes: int
    pod_count: int
    node_count: int
    updated_at: datetime


class RegistryConflictError(RuntimeError):
    """Raised when a registry CAS write sees a stale resource version."""

    def __init__(self, app_name: str, *, expected: int, actual: int) -> None:
        self.app_name = str(app_name)
        self.expected = int(expected)
        self.actual = int(actual)
        super().__init__(
            f"registry resourceVersion conflict for {self.app_name}: "
            f"expected={self.expected} actual={self.actual}"
        )


@dataclass(slots=True)
class PodStatus:
    """Status for a single pod in the state store."""

    ready: bool
    live: bool
    status: str
    readiness_message: str
    liveness_message: str
    pod_name: str = ""
    endpoint: str | None = None
    exit_code: int | None = None
    finished_at: datetime | None = None
    replica_id: InitVar[str | None] = None

    def __post_init__(self, replica_id: str | None) -> None:
        if not self.pod_name and replica_id:
            self.pod_name = str(replica_id)

    @property
    def replica_id(self) -> str:
        return self.pod_name

    @replica_id.setter
    def replica_id(self, value: str) -> None:
        self.pod_name = value


ReplicaStatus = PodStatus


@dataclass(slots=True)
class ProbeHistoryEntry:
    """Recorded probe evaluation for audit/history purposes."""

    check_time: datetime
    ready: bool
    live: bool
    readiness_message: str
    liveness_message: str
    pod_name: str = ""
    replica_id: InitVar[str | None] = None

    def __post_init__(self, replica_id: str | None) -> None:
        if not self.pod_name and replica_id:
            self.pod_name = str(replica_id)

    @property
    def replica_id(self) -> str:
        return self.pod_name

    @replica_id.setter
    def replica_id(self, value: str) -> None:
        self.pod_name = value


@dataclass(slots=True)
class AppEvent:
    """Event emitted during reconciliation or runtime changes."""

    app_name: str
    revision: int
    event_type: str
    message: str
    created_at: datetime


@dataclass(slots=True)
class WorkQueueLease:
    """Leased work item for lab-edge work.pull."""

    work_id: str
    attempt: int
    site_id: str
    payload: dict
    lease_id: str
    lease_expires_at: datetime | None


@dataclass(slots=True)
class NodeLease:
    """Lease record for a node (lab-edge semantics)."""

    node_id: str
    site_id: str
    session_id: str
    lease_id: str
    controller_epoch: int
    lease_ttl_ms: int
    renew_after_ms: int
    last_renew_at: datetime
    expires_at: datetime


@dataclass(slots=True)
class SiteIngressEndpoint:
    """Ingress endpoint metadata for a site (core-proxy/core-to-edge-public)."""

    site_id: str
    mode: str
    core_proxy_port: int | None
    public_urls: list[str | dict]
    quarantine_until: datetime | None
    created_at: datetime
    updated_at: datetime


@dataclass(slots=True)
class SiteIngressListItem:
    site_id: str
    mode: str
    core_proxy_port: int | None
    public_urls: list[str | dict]
    quarantine_until: datetime | None


@dataclass(slots=True)
class EdgeIngressRouteRecord:
    name: str
    namespace: str
    site_id: str
    policy_name: str | None
    policy_namespace: str | None
    spec: dict
    status: dict | None
    created_at: datetime
    updated_at: datetime


@dataclass(slots=True)
class EdgeIngressPolicyRecord:
    name: str
    namespace: str
    spec: dict
    status: dict | None
    created_at: datetime
    updated_at: datetime


@dataclass(slots=True)
class WorkOutboxEntry:
    work_id: str
    attempt: int
    site_id: str
    payload: dict
    publish_subject: str
    publish_msg_id: str
    publish_attempts: int
    last_publish_at: datetime | None = None
    last_publish_error: str | None = None


@dataclass(slots=True)
class WorkLedgerEntry:
    work_id: str
    attempt: int
    site_id: str
    state: str
    controller_id: str | None
    controller_epoch: int | None
    operation_id: str | None
    desired_generation: int | None
    assigned_node_id: str | None
    observed_generation: int | None
    result: dict | None
    created_at: datetime
    updated_at: datetime
    state_updated_at: datetime


@dataclass(slots=True)
class ServiceRecord:
    """Service-level metadata such as ClusterIP and exposed ports."""

    app_name: str
    cluster_ip: str
    ports: dict


@dataclass(slots=True)
class ServiceEndpoint:
    """Endpoint backing a Service port."""

    app_name: str
    port: int
    ip: str
    target_port: int
    ready: bool


@dataclass(slots=True)
class ServiceListItem:
    """Brief view used for IP allocation."""

    app_name: str
    cluster_ip: str


@dataclass(slots=True)
class VolumeAttachment:
    """Attachment record for a volume bound to a node."""

    app_name: str
    volume_name: str
    node_id: str
    retention: str | None
    created_at: datetime


@dataclass(slots=True)
class NodeRecord:
    """Registered node information."""

    node_id: str
    name: str | None
    labels: dict
    capabilities: dict
    taints: list
    backend: str | None
    endpoint: str | None
    pod_cidr: str | None
    wg_pubkey: str | None
    rp_pubkey: str | None
    cordoned: bool
    created_at: datetime
    updated_at: datetime


@dataclass(slots=True)
class NodeStatus:
    """Latest heartbeat/status for a node."""

    node_id: str
    status: str
    seen_at: datetime


@dataclass(slots=True)
class RevisionInfo:
    """Information about a stored application revision."""

    revision: int
    spec_hash: str
    status: str
    image: str
    created_at: datetime | None = None


@dataclass(slots=True)
class InferenceCellRecord:
    """Stored InferenceCell desired+observed state."""

    cell_key: str
    namespace: str
    cell_id: str
    manifest: InferenceCellManifest
    phase: str
    tp: int
    pp: int
    executor_type: str
    ray_scope: str
    allocations: dict
    admission: dict
    conditions: dict
    restarts: int
    last_error: str | None
    source: str
    updated_at: datetime


@dataclass(slots=True)
class InferenceCellEvent:
    """Event emitted for inference cell reconciliation."""

    cell_key: str
    event_type: str
    message: str
    created_at: datetime


@dataclass(slots=True)
class InferenceCellSetRecord:
    """Stored InferenceCellSet template and rollout status."""

    set_key: str
    namespace: str
    name: str
    manifest: InferenceCellSetManifest
    desired: int
    current: int
    ready: int
    last_error: str | None
    source: str
    updated_at: datetime


@dataclass(slots=True)
class FabricSessionRecord:
    """Persisted per-cell fabric session metadata."""

    session_id: str
    cell_key: str
    policy_mode: str
    members: list[dict]
    allowed_rules: list[dict]
    status: str
    expires_at: datetime | None
    created_at: datetime
    updated_at: datetime


@dataclass(slots=True)
class AIRuntimeProfileRecord:
    """Stored non-authoritative AI runtime profile evidence."""

    run_id: str
    track: str
    profile: dict
    admission: dict
    workerbee_status: dict | None
    warning_codes: list[str]
    admitted: bool
    promotion_ready: bool
    created_at: datetime
    updated_at: datetime


class SQLiteStateStore:
    """Minimal state store; sqlite by default, Postgres via AE_STATE_DSN or dsn=."""

    def __init__(self, db_path: Path | None = None, *, dsn: str | None = None) -> None:
        self._dsn = dsn or os.getenv("AE_STATE_DSN")
        self._db_path = db_path or Path("state/controller.db")
        self.backend = "sqlite"
        if self._dsn:
            if psycopg is None:
                raise RuntimeError(
                    "psycopg is required for Postgres state store (install psycopg[binary])"
                )
            self.backend = "postgres"
        self._initialize()

    def _initialize(self) -> None:
        with self._connect() as conn:
            # Drop legacy replica tables now that pod naming is canonical.
            conn.execute("DROP TABLE IF EXISTS replica_nodes")
            conn.execute("DROP TABLE IF EXISTS replica_status")
            # Best-effort schema upgrades before strict checks.
            try:
                self._ensure_column(conn, "pod_status", "endpoint", "TEXT")
            except Exception:
                pass
            try:
                self._ensure_column(conn, "pod_status", "updated_at", "TEXT")
            except Exception:
                pass
            for column in (
                "current_revision_ready_replicas",
                "current_revision_live_replicas",
                "old_revision_ready_replicas",
                "old_revision_live_replicas",
                "overlap_ready_replicas",
                "overlap_live_replicas",
            ):
                try:
                    self._ensure_column(conn, "app_status", column, "INTEGER NOT NULL DEFAULT 0")
                except Exception:
                    pass
            try:
                self._ensure_column(conn, "nodes", "rp_pubkey", "TEXT")
            except Exception:
                pass
            try:
                self._ensure_column(conn, "nodes", "capabilities_json", "TEXT")
            except Exception:
                pass
            needs_reset = not self._schema_matches(
                conn,
                "app_status",
                [
                    "app_name",
                    "desired_replicas",
                    "ready_replicas",
                    "live_replicas",
                    "revision",
                    "revision_status",
                    "image",
                    "created",
                    "updated",
                    "removed",
                    "current_revision_ready_replicas",
                    "current_revision_live_replicas",
                    "old_revision_ready_replicas",
                    "old_revision_live_replicas",
                    "overlap_ready_replicas",
                    "overlap_live_replicas",
                    "ingress_host",
                    "ingress_path",
                ],
            ) or not self._schema_matches(
                conn,
                "pod_status",
                [
                    "app_name",
                    "pod_name",
                    "ready",
                    "live",
                    "endpoint",
                    "status",
                    "readiness_message",
                    "liveness_message",
                    "exit_code",
                    "finished_at",
                    "updated_at",
                ],
            )
            if needs_reset:
                conn.execute("DROP TABLE IF EXISTS probe_history")
                conn.execute("DROP TABLE IF EXISTS pod_status")
                conn.execute("DROP TABLE IF EXISTS app_status")
            elif not self._schema_matches(
                conn,
                "probe_history",
                [
                    "id",
                    "app_name",
                    "pod_name",
                    "check_time",
                    "ready",
                    "live",
                    "readiness_message",
                    "liveness_message",
                ],
            ):
                conn.execute("DROP TABLE IF EXISTS probe_history")

            # Service tables are additive; drop and recreate only if schema mismatches.
            if not self._schema_matches(
                conn,
                "services",
                [
                    "app_name",
                    "cluster_ip",
                    "ports",
                    "created_at",
                ],
            ):
                conn.execute("DROP TABLE IF EXISTS services")
            if not self._schema_matches(
                conn,
                "service_endpoints",
                [
                    "app_name",
                    "port",
                    "ip",
                    "target_port",
                    "ready",
                ],
            ):
                conn.execute("DROP TABLE IF EXISTS service_endpoints")
            if not self._schema_matches(
                conn,
                "pod_nodes",
                [
                    "app_name",
                    "pod_name",
                    "node_id",
                    "updated_at",
                ],
            ):
                conn.execute("DROP TABLE IF EXISTS pod_nodes")
            if not self._schema_matches(
                conn,
                "nodes",
                [
                    "node_id",
                    "name",
                    "labels",
                    "taints",
                    "backend",
                    "endpoint",
                    "pod_cidr",
                    "wg_pubkey",
                    "cordoned",
                    "created_at",
                    "updated_at",
                    "rp_pubkey",
                    "capabilities_json",
                ],
            ):
                conn.execute("DROP TABLE IF EXISTS nodes")
            if not self._schema_matches(
                conn,
                "node_heartbeats",
                [
                    "node_id",
                    "status",
                    "seen_at",
                ],
            ):
                conn.execute("DROP TABLE IF EXISTS node_heartbeats")
            if not self._schema_matches(
                conn,
                "volume_attachments",
                [
                    "app_name",
                    "volume_name",
                    "node_id",
                    "retention",
                    "created_at",
                ],
            ):
                conn.execute("DROP TABLE IF EXISTS volume_attachments")
            auto_inc = (
                "INTEGER PRIMARY KEY AUTOINCREMENT"
                if self.backend == "sqlite"
                else "SERIAL PRIMARY KEY"
            )
            conn.execute(resource_loader.load_text("sql", "controller", "create_app_status.sql"))
            conn.execute(resource_loader.load_text("sql", "controller", "create_app_registry.sql"))
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS authority_objects (
                  grp TEXT NOT NULL,
                  ver TEXT NOT NULL,
                  resource TEXT NOT NULL,
                  namespace TEXT NOT NULL,
                  name TEXT NOT NULL,
                  kind TEXT NOT NULL,
                  metadata_json TEXT NOT NULL,
                  spec_json TEXT NOT NULL,
                  status_json TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  resource_version INTEGER NOT NULL DEFAULT 0,
                  PRIMARY KEY (grp, ver, resource, namespace, name)
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_authority_objects_gvr
                ON authority_objects (grp, ver, resource, namespace, name)
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS workload_metrics_snapshots (
                  app_name TEXT PRIMARY KEY,
                  controller_id TEXT NOT NULL,
                  controller_epoch INTEGER NOT NULL,
                  collected_at TEXT NOT NULL,
                  cpu_utilization REAL,
                  memory_utilization REAL,
                  memory_bytes INTEGER NOT NULL,
                  pod_count INTEGER NOT NULL,
                  node_count INTEGER NOT NULL,
                  updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(resource_loader.load_text("sql", "controller", "create_pod_status.sql"))
            conn.execute(
                resource_loader.render_text(
                    "sql",
                    "controller",
                    "create_probe_history.sql",
                    AUTO_INC=auto_inc,
                )
            )
            conn.execute(resource_loader.load_text("sql", "controller", "create_pod_nodes.sql"))
            conn.execute(resource_loader.load_text("sql", "controller", "create_app_revisions.sql"))
            conn.execute(
                resource_loader.render_text(
                    "sql",
                    "controller",
                    "create_app_events.sql",
                    AUTO_INC=auto_inc,
                )
            )
            conn.execute(
                resource_loader.load_text("sql", "controller", "create_rollout_canary.sql")
            )
            conn.execute(resource_loader.load_text("sql", "controller", "create_services.sql"))
            conn.execute(
                resource_loader.load_text("sql", "controller", "create_service_endpoints.sql")
            )
            conn.execute(resource_loader.load_text("sql", "controller", "create_nodes.sql"))
            conn.execute(
                resource_loader.load_text("sql", "controller", "create_node_heartbeats.sql")
            )
            self._execute_script(
                conn,
                resource_loader.load_text("sql", "controller", "create_node_leases.sql"),
            )
            conn.execute(
                resource_loader.load_text("sql", "controller", "create_volume_attachments.sql")
            )
            self._execute_script(
                conn,
                resource_loader.load_text("sql", "controller", "create_work_queue.sql"),
            )
            self._execute_script(
                conn,
                resource_loader.load_text("sql", "controller", "create_work_outbox.sql"),
            )
            self._execute_script(
                conn,
                resource_loader.load_text("sql", "controller", "create_work_ledger.sql"),
            )
            self._execute_script(
                conn,
                resource_loader.load_text("sql", "controller", "create_site_ingress_endpoints.sql"),
            )
            self._execute_script(
                conn,
                resource_loader.load_text("sql", "controller", "create_edge_ingress_routes.sql"),
            )
            self._execute_script(
                conn,
                resource_loader.load_text("sql", "controller", "create_edge_ingress_policies.sql"),
            )
            self._ensure_column(
                conn,
                "app_registry",
                "resource_version",
                "INTEGER NOT NULL DEFAULT 0",
            )
            conn.execute(
                "UPDATE app_registry SET resource_version = 1 WHERE COALESCE(resource_version, 0) < 1"
            )
            self._ensure_column(conn, "edge_ingress_routes", "status_json", "TEXT")
            self._ensure_column(conn, "edge_ingress_policies", "status_json", "TEXT")
            self._ensure_column(conn, "pod_status", "endpoint", "TEXT")
            self._ensure_column(conn, "work_ledger", "controller_id", "TEXT")
            self._ensure_column(conn, "work_ledger", "controller_epoch", "INTEGER")
            self._ensure_column(conn, "work_ledger", "operation_id", "TEXT")
            self._ensure_column(conn, "work_outbox", "publish_subject", "TEXT")
            self._ensure_column(conn, "work_outbox", "publish_msg_id", "TEXT")
            self._ensure_column(conn, "work_outbox", "last_publish_error", "TEXT")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS inference_cells (
                  cell_key TEXT PRIMARY KEY,
                  namespace TEXT NOT NULL,
                  cell_id TEXT NOT NULL,
                  spec_json TEXT NOT NULL,
                  model_id TEXT,
                  tp INTEGER NOT NULL,
                  pp INTEGER NOT NULL,
                  executor_type TEXT NOT NULL,
                  ray_scope TEXT NOT NULL,
                  phase TEXT NOT NULL,
                  allocations_json TEXT NOT NULL,
                  admission_json TEXT NOT NULL,
                  conditions_json TEXT NOT NULL,
                  restarts INTEGER NOT NULL DEFAULT 0,
                  last_error TEXT,
                  source TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_inference_cells_namespace ON inference_cells(namespace)"
            )
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS inference_cell_events (
                  id {auto_inc},
                  cell_key TEXT NOT NULL,
                  event_type TEXT NOT NULL,
                  message TEXT NOT NULL,
                  created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_inference_cell_events_key ON inference_cell_events(cell_key)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS inference_cell_sets (
                  set_key TEXT PRIMARY KEY,
                  namespace TEXT NOT NULL,
                  name TEXT NOT NULL,
                  spec_json TEXT NOT NULL,
                  desired INTEGER NOT NULL DEFAULT 0,
                  current INTEGER NOT NULL DEFAULT 0,
                  ready INTEGER NOT NULL DEFAULT 0,
                  last_error TEXT,
                  source TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_inference_cell_sets_namespace ON inference_cell_sets(namespace)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS inference_fabric_sessions (
                  session_id TEXT PRIMARY KEY,
                  cell_key TEXT NOT NULL,
                  policy_mode TEXT NOT NULL,
                  members_json TEXT NOT NULL,
                  allowed_rules_json TEXT NOT NULL,
                  status TEXT NOT NULL,
                  expires_at TEXT,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_inference_fabric_sessions_cell ON inference_fabric_sessions(cell_key)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS ai_runtime_profiles (
                  run_id TEXT PRIMARY KEY,
                  track TEXT NOT NULL,
                  profile_json TEXT NOT NULL,
                  admission_json TEXT NOT NULL,
                  workerbee_status_json TEXT,
                  warning_codes_json TEXT NOT NULL,
                  admitted INTEGER NOT NULL,
                  promotion_ready INTEGER NOT NULL,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_ai_runtime_profiles_track_updated
                ON ai_runtime_profiles(track, updated_at DESC)
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS inference_gpu_leases (
                  node_id TEXT NOT NULL,
                  gpu_index INTEGER NOT NULL,
                  lease_id TEXT NOT NULL,
                  cell_key TEXT NOT NULL,
                  expires_at TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  PRIMARY KEY (node_id, gpu_index)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_inference_gpu_leases_lease ON inference_gpu_leases(lease_id)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS inference_port_leases (
                  node_id TEXT NOT NULL,
                  port INTEGER NOT NULL,
                  lease_id TEXT NOT NULL,
                  cell_key TEXT NOT NULL,
                  expires_at TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  PRIMARY KEY (node_id, port)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_inference_port_leases_lease ON inference_port_leases(lease_id)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS inference_node_locks (
                  node_id TEXT PRIMARY KEY,
                  lease_id TEXT NOT NULL,
                  cell_key TEXT NOT NULL,
                  expires_at TEXT NOT NULL,
                  created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS fabric_chunks (
                  chunk_id TEXT PRIMARY KEY,
                  namespace TEXT NOT NULL,
                  name TEXT NOT NULL,
                  digest TEXT NOT NULL,
                  size_bytes INTEGER NOT NULL DEFAULT 0,
                  source_kind TEXT NOT NULL,
                  source_ref TEXT NOT NULL,
                  labels_json TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_fabric_chunks_namespace ON fabric_chunks(namespace, name)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS fabric_residencies (
                  chunk_id TEXT NOT NULL,
                  node_id TEXT NOT NULL,
                  storage_device_id TEXT NOT NULL,
                  path TEXT NOT NULL,
                  state TEXT NOT NULL,
                  integrity_state TEXT NOT NULL,
                  epoch INTEGER NOT NULL DEFAULT 0,
                  digest TEXT NOT NULL,
                  verified_at TEXT,
                  updated_at TEXT NOT NULL,
                  PRIMARY KEY (chunk_id, node_id)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_fabric_residencies_node ON fabric_residencies(node_id)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS fabric_movements (
                  movement_id TEXT PRIMARY KEY,
                  chunk_id TEXT NOT NULL,
                  direction TEXT NOT NULL,
                  source_node_id TEXT NOT NULL,
                  target_node_id TEXT NOT NULL,
                  status TEXT NOT NULL,
                  requested_by TEXT NOT NULL,
                  digest TEXT NOT NULL,
                  epoch INTEGER NOT NULL DEFAULT 0,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  started_at TEXT,
                  finished_at TEXT,
                  error TEXT
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_fabric_movements_chunk ON fabric_movements(chunk_id, created_at)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS fabric_advisory_requests (
                  request_id TEXT PRIMARY KEY,
                  subject_type TEXT NOT NULL,
                  subject_id TEXT NOT NULL,
                  intent TEXT NOT NULL,
                  facts_ref TEXT NOT NULL,
                  locality_snapshot_ref TEXT NOT NULL,
                  max_candidates INTEGER NOT NULL DEFAULT 0,
                  time_budget_ms INTEGER NOT NULL DEFAULT 0,
                  policy_mode TEXT NOT NULL,
                  created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_fabric_advisory_requests_subject ON fabric_advisory_requests(subject_type, subject_id)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS fabric_advisory_responses (
                  request_id TEXT PRIMARY KEY,
                  provider TEXT NOT NULL,
                  status TEXT NOT NULL,
                  recommendation TEXT NOT NULL,
                  confidence REAL,
                  evidence_refs_json TEXT NOT NULL,
                  authoritative INTEGER NOT NULL DEFAULT 0,
                  created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS fabric_decision_traces (
                  trace_id TEXT PRIMARY KEY,
                  request_id TEXT NOT NULL,
                  deterministic_baseline_json TEXT NOT NULL,
                  advisory_response_json TEXT NOT NULL,
                  accepted INTEGER,
                  divergence_reason TEXT,
                  replay_status TEXT NOT NULL,
                  continuity_signals_json TEXT NOT NULL,
                  coherence_signals_json TEXT NOT NULL,
                  created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_fabric_decision_traces_request ON fabric_decision_traces(request_id, created_at)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS fabric_transfer_capabilities (
                  capability_id TEXT PRIMARY KEY,
                  node_id TEXT NOT NULL,
                  peer_node_id TEXT NOT NULL,
                  transport TEXT NOT NULL,
                  status TEXT NOT NULL,
                  priority INTEGER NOT NULL DEFAULT 0,
                  capabilities_json TEXT NOT NULL,
                  fallback_transport TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_fabric_transfer_capabilities_node ON fabric_transfer_capabilities(node_id, peer_node_id)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS fabric_transfer_leases (
                  lease_id TEXT PRIMARY KEY,
                  chunk_id TEXT NOT NULL,
                  source_node_id TEXT NOT NULL,
                  target_node_id TEXT NOT NULL,
                  transport TEXT NOT NULL,
                  status TEXT NOT NULL,
                  holder TEXT NOT NULL,
                  landing_zone_id TEXT NOT NULL,
                  digest TEXT NOT NULL,
                  epoch INTEGER NOT NULL DEFAULT 0,
                  expires_at TEXT,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_fabric_transfer_leases_chunk ON fabric_transfer_leases(chunk_id, created_at)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS fabric_landing_zones (
                  zone_id TEXT PRIMARY KEY,
                  node_id TEXT NOT NULL,
                  path TEXT NOT NULL,
                  capacity_bytes INTEGER NOT NULL DEFAULT 0,
                  reserved_bytes INTEGER NOT NULL DEFAULT 0,
                  safety_state TEXT NOT NULL,
                  cleanup_policy TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_fabric_landing_zones_node ON fabric_landing_zones(node_id)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS fabric_transport_attempts (
                  attempt_id TEXT PRIMARY KEY,
                  lease_id TEXT NOT NULL,
                  chunk_id TEXT NOT NULL,
                  transport TEXT NOT NULL,
                  status TEXT NOT NULL,
                  fallback_used INTEGER NOT NULL DEFAULT 0,
                  fallback_transport TEXT NOT NULL,
                  error TEXT,
                  started_at TEXT,
                  finished_at TEXT,
                  created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_fabric_transport_attempts_lease ON fabric_transport_attempts(lease_id, created_at)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS fabric_das_cell_bundles (
                  bundle_id TEXT PRIMARY KEY,
                  site_id TEXT NOT NULL,
                  cell_id TEXT NOT NULL,
                  version TEXT NOT NULL,
                  storage_ref TEXT NOT NULL,
                  facts_ref TEXT NOT NULL,
                  status TEXT NOT NULL,
                  labels_json TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_fabric_das_cell_bundles_site ON fabric_das_cell_bundles(site_id, cell_id)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS fabric_das_query_traces (
                  trace_id TEXT PRIMARY KEY,
                  bundle_id TEXT NOT NULL,
                  site_id TEXT NOT NULL,
                  query_id TEXT NOT NULL,
                  query_kind TEXT NOT NULL,
                  local_first INTEGER NOT NULL DEFAULT 0,
                  warmed_refs_json TEXT NOT NULL,
                  promoted_refs_json TEXT NOT NULL,
                  fallback_sites_json TEXT NOT NULL,
                  result_ref TEXT NOT NULL,
                  created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_fabric_das_query_traces_bundle ON fabric_das_query_traces(bundle_id, created_at)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS fabric_das_replications (
                  replication_id TEXT PRIMARY KEY,
                  bundle_id TEXT NOT NULL,
                  source_site_id TEXT NOT NULL,
                  target_site_id TEXT NOT NULL,
                  mode TEXT NOT NULL,
                  status TEXT NOT NULL,
                  approved_by TEXT NOT NULL,
                  reason TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_fabric_das_replications_bundle ON fabric_das_replications(bundle_id, created_at)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS fabric_cognitive_signals (
                  signal_id TEXT PRIMARY KEY,
                  subject_type TEXT NOT NULL,
                  subject_id TEXT NOT NULL,
                  signal_kind TEXT NOT NULL,
                  continuity_ref TEXT NOT NULL,
                  coherence_score REAL,
                  overload_state TEXT NOT NULL,
                  review_gate TEXT NOT NULL,
                  advisory_trace_id TEXT NOT NULL,
                  created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_fabric_cognitive_signals_subject ON fabric_cognitive_signals(subject_type, subject_id, created_at)"
            )
            conn.commit()

    def _execute_script(self, conn, sql: str) -> None:
        if self.backend == "sqlite":
            conn.executescript(sql)
            return
        for stmt in sql.split(";"):
            stmt = stmt.strip()
            if stmt:
                conn.execute(stmt)

    def _ensure_column(self, conn, table: str, column: str, col_type: str) -> None:
        if self.backend == "sqlite":
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
            except Exception:
                return
            return
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {col_type}")
        except Exception:
            return

    def _connect(self):
        if self.backend == "sqlite":
            conn = sqlite3.connect(self._db_path)
            conn.execute("PRAGMA foreign_keys = ON")
            return conn
        raw = psycopg.connect(self._dsn)  # type: ignore[arg-type]
        return _PgCompatConnection(raw)

    def _schema_matches(self, conn, table: str, expected_columns: list[str]) -> bool:
        if self.backend == "sqlite":
            info = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            if info is None:
                return False
            columns = [row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]
            return columns == expected_columns
        # For Postgres we skip strict schema match; rely on CREATE IF NOT EXISTS.
        return True

    def record_snapshot(
        self,
        manifest: AppManifest,
        runtime_result: RuntimeResult,
        health_report: HealthReport,
        revision: int,
        revision_status: str,
        *,
        current_revision_ready_replicas: int = 0,
        current_revision_live_replicas: int = 0,
        old_revision_ready_replicas: int = 0,
        old_revision_live_replicas: int = 0,
        overlap_ready_replicas: int = 0,
        overlap_live_replicas: int = 0,
    ) -> None:
        state_by_id = {state.pod_name: state for state in runtime_result.pod_states}
        app_name = app_key_for_manifest(manifest)

        with self._connect() as conn:
            conn.execute(
                resource_loader.load_text("sql", "controller", "upsert_app_status.sql"),
                (
                    app_name,
                    manifest.spec.replicas,
                    health_report.ready_replicas,
                    health_report.live_replicas,
                    revision,
                    revision_status,
                    manifest.spec.image,
                    runtime_result.created,
                    runtime_result.updated,
                    runtime_result.removed,
                    int(current_revision_ready_replicas),
                    int(current_revision_live_replicas),
                    int(old_revision_ready_replicas),
                    int(old_revision_live_replicas),
                    int(overlap_ready_replicas),
                    int(overlap_live_replicas),
                    manifest.spec.ingress.host if manifest.spec.ingress else None,
                    manifest.spec.ingress.path if manifest.spec.ingress else None,
                ),
            )

            # Preserve existing placements to avoid dashboard flicker; refresh timestamps
            # for pods that still exist, and prune by TTL instead of per-snapshot deletion.
            current_pods = [pod.pod_name for pod in health_report.pods if pod.pod_name]
            ts = datetime.now(timezone.utc).isoformat()
            if current_pods:
                placeholders = ",".join("?" for _ in current_pods)
                conn.execute(
                    f"UPDATE pod_nodes SET updated_at = ? WHERE app_name = ? AND pod_name IN ({placeholders})",
                    (ts, app_name, *current_pods),
                )

            rows = []
            for pod in health_report.pods:
                state = state_by_id.get(pod.pod_name)
                rows.append(
                    (
                        app_name,
                        pod.pod_name,
                        int(pod.ready),
                        int(pod.live),
                        state.endpoint if state else None,
                        state.status if state else "unknown",
                        pod.readiness_message,
                        pod.liveness_message,
                        state.exit_code if state else None,
                        state.finished_at.isoformat() if state and state.finished_at else None,
                        ts,
                    )
                )
            if rows:
                conn.executemany(
                    resource_loader.load_text("sql", "controller", "insert_pod_status.sql"),
                    rows,
                )

            try:
                ttl_seconds = int(os.getenv("AE_POD_STATUS_TTL_SECONDS", "30") or "30")
            except Exception:
                ttl_seconds = 30
            if ttl_seconds > 0:
                cutoff = datetime.now(timezone.utc) - timedelta(seconds=ttl_seconds)
                conn.execute(
                    "DELETE FROM pod_status WHERE app_name = ? AND (updated_at IS NULL OR updated_at < ?)",
                    (app_name, cutoff.isoformat()),
                )
            try:
                node_ttl_seconds = int(os.getenv("AE_POD_NODE_TTL_SECONDS", "300") or "300")
            except Exception:
                node_ttl_seconds = 300
            if node_ttl_seconds > 0:
                cutoff = datetime.now(timezone.utc) - timedelta(seconds=node_ttl_seconds)
                conn.execute(
                    "DELETE FROM pod_nodes WHERE app_name = ? AND (updated_at IS NULL OR updated_at < ?)",
                    (app_name, cutoff.isoformat()),
                )

            timestamp = datetime.now(timezone.utc).isoformat()
            history_rows = [
                (
                    app_name,
                    pod.pod_name,
                    timestamp,
                    int(pod.ready),
                    int(pod.live),
                    pod.readiness_message,
                    pod.liveness_message,
                )
                for pod in health_report.pods
            ]
            if history_rows:
                conn.executemany(
                    resource_loader.load_text("sql", "controller", "insert_probe_history.sql"),
                    history_rows,
                )
                for pod in health_report.pods:
                    conn.execute(
                        resource_loader.load_text(
                            "sql", "controller", "delete_probe_history_limit.sql"
                        ),
                        (app_name, pod.pod_name),
                    )
            # Persist placement mapping when runtime result contains node_id hints
            node_rows = []
            for rs in runtime_result.pod_states:
                node_id = getattr(rs, "node_id", None)
                if not node_id:
                    continue
                node_rows.append(
                    (
                        app_name,
                        rs.pod_name,
                        node_id,
                        timestamp,
                    )
                )
            if node_rows:
                conn.executemany(
                    resource_loader.load_text("sql", "controller", "insert_pod_nodes_upsert.sql"),
                    node_rows,
                )
            conn.execute(
                resource_loader.load_text("sql", "controller", "update_app_revisions_status.sql"),
                (revision_status, manifest.spec.image, app_name, revision),
            )
            conn.commit()

    def get_status(self, app_name: str) -> AppStatus | None:
        with self._connect() as conn:
            row = conn.execute(
                resource_loader.load_text("sql", "controller", "select_app_status_by_name.sql"),
                (app_name,),
            ).fetchone()
            if row is None:
                return None
            return AppStatus(
                app_name=row[0],
                desired_replicas=row[1],
                ready_replicas=row[2],
                live_replicas=row[3],
                revision=row[4],
                revision_status=row[5],
                image=row[6],
                created=row[7],
                updated=row[8],
                removed=row[9],
                current_revision_ready_replicas=row[10],
                current_revision_live_replicas=row[11],
                old_revision_ready_replicas=row[12],
                old_revision_live_replicas=row[13],
                overlap_ready_replicas=row[14],
                overlap_live_replicas=row[15],
                ingress_host=row[16],
                ingress_path=row[17],
            )

    def list_status(self) -> list[AppStatus]:
        with self._connect() as conn:
            rows = conn.execute(
                resource_loader.load_text("sql", "controller", "select_app_status_all.sql")
            ).fetchall()
        return [
            AppStatus(
                app_name=row[0],
                desired_replicas=row[1],
                ready_replicas=row[2],
                live_replicas=row[3],
                revision=row[4],
                revision_status=row[5],
                image=row[6],
                created=row[7],
                updated=row[8],
                removed=row[9],
                current_revision_ready_replicas=row[10],
                current_revision_live_replicas=row[11],
                old_revision_ready_replicas=row[12],
                old_revision_live_replicas=row[13],
                overlap_ready_replicas=row[14],
                overlap_live_replicas=row[15],
                ingress_host=row[16],
                ingress_path=row[17],
            )
            for row in rows
        ]

    def list_pods(self, app_name: str) -> list[PodStatus]:
        with self._connect() as conn:
            rows = conn.execute(
                resource_loader.load_text("sql", "controller", "select_pod_status_by_app.sql"),
                (app_name,),
            ).fetchall()
        items: list[PodStatus] = []
        for row in rows:
            finished_at = None
            if row[8]:
                try:
                    finished_at = datetime.fromisoformat(row[8])
                except Exception:
                    finished_at = None
            items.append(
                PodStatus(
                    pod_name=row[0],
                    ready=bool(row[1]),
                    live=bool(row[2]),
                    status=row[4],
                    endpoint=row[3],
                    readiness_message=row[5],
                    liveness_message=row[6],
                    exit_code=row[7] if row[7] is not None else None,
                    finished_at=finished_at,
                )
            )
        return items

    def list_pod_nodes(self, app_name: str) -> list[tuple[str, str]]:
        with self._connect() as conn:
            rows = conn.execute(
                resource_loader.load_text("sql", "controller", "select_pod_nodes_with_status.sql"),
                (app_name, app_name),
            ).fetchall()
        return [(row[0], row[1], row[2], row[3], row[4], row[5], row[6]) for row in rows]

    def set_pod_nodes(self, app_name: str, placements: list[tuple[str, str]]) -> None:
        """Replace placement mapping for an app."""
        ts = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute("DELETE FROM pod_nodes WHERE app_name = ?", (app_name,))
            if placements:
                conn.executemany(
                    resource_loader.load_text("sql", "controller", "insert_pod_nodes.sql"),
                    [(app_name, rid, nid, ts) for rid, nid in placements],
                )
            conn.commit()

    def get_probe_history(self, app_name: str, limit: int) -> list[ProbeHistoryEntry]:
        with self._connect() as conn:
            rows = conn.execute(
                resource_loader.load_text("sql", "controller", "select_probe_history_by_app.sql"),
                (app_name, limit),
            ).fetchall()
        entries: list[ProbeHistoryEntry] = []
        for row in rows:
            try:
                check_time = datetime.fromisoformat(row[1])
            except ValueError:
                check_time = datetime.fromtimestamp(0, tz=timezone.utc)
            entries.append(
                ProbeHistoryEntry(
                    pod_name=row[0],
                    check_time=check_time,
                    ready=bool(row[2]),
                    live=bool(row[3]),
                    readiness_message=row[4],
                    liveness_message=row[5],
                )
            )
        return entries

    def prepare_revision(self, manifest: AppManifest, spec_hash: str) -> tuple[int, bool]:
        app_name = app_key_for_manifest(manifest)
        latest = self._get_latest_revision(app_name)
        if latest and latest.spec_hash == spec_hash:
            return latest.revision, False

        next_revision = (latest.revision if latest else 0) + 1 if latest else 1
        spec_json = json.dumps(manifest.model_dump(by_alias=True), sort_keys=True)
        created_at = datetime.now(timezone.utc).isoformat()

        with self._connect() as conn:
            conn.execute(
                resource_loader.load_text("sql", "controller", "insert_app_revisions.sql"),
                (
                    app_name,
                    next_revision,
                    spec_hash,
                    spec_json,
                    manifest.spec.image,
                    created_at,
                    "pending",
                ),
            )
            conn.commit()
        return next_revision, True

    def _manifest_hash(self, manifest: AppManifest) -> str:
        payload = json.dumps(
            manifest.model_dump(by_alias=True, exclude_none=True),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def register_app(
        self,
        manifest: AppManifest,
        *,
        source: str | None = None,
        labels: dict | None = None,
        expected_resource_version: int | None = None,
    ) -> int:
        spec_json = json.dumps(manifest.model_dump(by_alias=True), sort_keys=True)
        spec_hash = self._manifest_hash(manifest)
        updated_at = datetime.now(timezone.utc).isoformat()
        app_name = app_key_for_manifest(manifest)
        existing_source = None
        existing_labels: dict | None = None
        current_rv = 0
        with self._connect() as conn:
            row = conn.execute(
                "SELECT source, labels, resource_version FROM app_registry WHERE app_name = ?",
                (app_name,),
            ).fetchone()
            if row is not None:
                existing_source = row[0]
                current_rv = int(row[2] or 0)
                try:
                    existing_labels = json.loads(row[1] or "{}")
                    if not isinstance(existing_labels, dict):
                        existing_labels = {}
                except Exception:
                    existing_labels = {}
            if expected_resource_version is not None:
                if current_rv != int(expected_resource_version):
                    raise RegistryConflictError(
                        app_name,
                        expected=int(expected_resource_version),
                        actual=current_rv,
                    )
            labels_json = json.dumps(
                labels if labels is not None else (existing_labels or {}),
                sort_keys=True,
            )
            source_val = str(source or existing_source or "unknown")
            next_resource_version = max(1, current_rv + 1)
            conn.execute(
                resource_loader.load_text("sql", "controller", "insert_app_registry_upsert.sql"),
                (
                    app_name,
                    spec_hash,
                    spec_json,
                    source_val,
                    labels_json,
                    updated_at,
                    next_resource_version,
                ),
            )
            conn.commit()
        return next_resource_version

    def list_registered_apps(self) -> list[RegistryEntry]:
        with self._connect() as conn:
            rows = conn.execute(
                resource_loader.load_text("sql", "controller", "select_app_registry_all.sql")
            ).fetchall()
        entries: list[RegistryEntry] = []
        for row in rows:
            entry = self._registry_entry_from_row(row)
            if entry is not None:
                entries.append(entry)
        return entries

    def list_registered_app_names(self) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute("SELECT app_name FROM app_registry ORDER BY app_name").fetchall()
        return [row[0] for row in rows]

    def get_registered_entry(self, app_name: str) -> RegistryEntry | None:
        with self._connect() as conn:
            row = conn.execute(
                resource_loader.load_text("sql", "controller", "select_app_registry_by_name.sql"),
                (app_name,),
            ).fetchone()
        if row is None:
            return None
        return self._registry_entry_from_row(row)

    def get_registered_manifest(self, app_name: str) -> AppManifest | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT spec_json FROM app_registry WHERE app_name = ?",
                (app_name,),
            ).fetchone()
        if row is None:
            return None
        try:
            return AppManifest.model_validate_json(row[0])
        except Exception:
            return None

    def delete_registered_app(
        self,
        app_name: str,
        *,
        expected_resource_version: int | None = None,
    ) -> bool:
        deleted = False
        with self._connect() as conn:
            if expected_resource_version is not None:
                row = conn.execute(
                    "SELECT resource_version FROM app_registry WHERE app_name = ?",
                    (app_name,),
                ).fetchone()
                current_rv = int(row[0] or 0) if row is not None else 0
                if current_rv != int(expected_resource_version):
                    raise RegistryConflictError(
                        app_name,
                        expected=int(expected_resource_version),
                        actual=current_rv,
                    )
            conn.execute("DELETE FROM app_registry WHERE app_name = ?", (app_name,))
            deleted = bool(getattr(conn, "total_changes", 0))
            conn.commit()
        return deleted

    def _registry_entry_from_row(self, row) -> RegistryEntry | None:
        try:
            manifest = AppManifest.model_validate_json(row[2])
        except Exception:
            return None
        try:
            labels = json.loads(row[4] or "{}")
            if not isinstance(labels, dict):
                labels = {}
        except Exception:
            labels = {}
        try:
            updated = datetime.fromisoformat(row[5]) if row[5] else datetime.now(timezone.utc)
        except Exception:
            updated = datetime.now(timezone.utc)
        return RegistryEntry(
            app_name=row[0],
            manifest=manifest,
            spec_hash=row[1],
            source=row[3],
            labels=labels,
            updated_at=updated,
            resource_version=int(row[6] or 0),
        )

    @staticmethod
    def _authority_object_conflict_key(
        group: str, version: str, resource: str, namespace: str | None, name: str
    ) -> str:
        return "/".join(
            [
                group or "core",
                version,
                resource,
                namespace or "_cluster",
                name,
            ]
        )

    def register_authority_object(
        self,
        group: str,
        version: str,
        resource: str,
        namespace: str | None,
        name: str,
        *,
        kind: str,
        metadata: dict | None = None,
        spec: dict | None = None,
        status: dict | None = None,
        expected_resource_version: int | None = None,
    ) -> int:
        metadata_json = json.dumps(metadata or {}, sort_keys=True)
        spec_json = json.dumps(spec or {}, sort_keys=True)
        status_json = json.dumps(status or {}, sort_keys=True)
        updated_at = datetime.now(timezone.utc).isoformat()
        ns_key = str(namespace or "")
        current_rv = 0
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT resource_version
                  FROM authority_objects
                 WHERE grp = ? AND ver = ? AND resource = ? AND namespace = ? AND name = ?
                """,
                (group, version, resource, ns_key, name),
            ).fetchone()
            if row is not None:
                current_rv = int(row[0] or 0)
            if expected_resource_version is not None and current_rv != int(expected_resource_version):
                raise RegistryConflictError(
                    self._authority_object_conflict_key(group, version, resource, namespace, name),
                    expected=int(expected_resource_version),
                    actual=current_rv,
                )
            next_resource_version = max(1, current_rv + 1)
            conn.execute(
                """
                INSERT INTO authority_objects (
                  grp,
                  ver,
                  resource,
                  namespace,
                  name,
                  kind,
                  metadata_json,
                  spec_json,
                  status_json,
                  updated_at,
                  resource_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(grp, ver, resource, namespace, name) DO UPDATE SET
                  kind = excluded.kind,
                  metadata_json = excluded.metadata_json,
                  spec_json = excluded.spec_json,
                  status_json = excluded.status_json,
                  updated_at = excluded.updated_at,
                  resource_version = excluded.resource_version
                """,
                (
                    group,
                    version,
                    resource,
                    ns_key,
                    name,
                    kind,
                    metadata_json,
                    spec_json,
                    status_json,
                    updated_at,
                    next_resource_version,
                ),
            )
            conn.commit()
        return next_resource_version

    def list_authority_objects(
        self,
        group: str,
        version: str,
        resource: str,
        namespace: str | None = None,
    ) -> list[AuthorityObjectEntry]:
        params: list[object] = [group, version, resource]
        query = (
            """
            SELECT grp, ver, resource, namespace, name, kind,
                   metadata_json, spec_json, status_json, updated_at, resource_version
              FROM authority_objects
             WHERE grp = ? AND ver = ? AND resource = ?
            """
        )
        if namespace is not None:
            query += " AND namespace = ?"
            params.append(str(namespace))
        query += " ORDER BY namespace, name"
        with self._connect() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
        out: list[AuthorityObjectEntry] = []
        for row in rows:
            entry = self._authority_object_from_row(row)
            if entry is not None:
                out.append(entry)
        return out

    def get_authority_object(
        self,
        group: str,
        version: str,
        resource: str,
        namespace: str | None,
        name: str,
    ) -> AuthorityObjectEntry | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT grp, ver, resource, namespace, name, kind,
                       metadata_json, spec_json, status_json, updated_at, resource_version
                  FROM authority_objects
                 WHERE grp = ? AND ver = ? AND resource = ? AND namespace = ? AND name = ?
                """,
                (group, version, resource, str(namespace or ""), name),
            ).fetchone()
        if row is None:
            return None
        return self._authority_object_from_row(row)

    def delete_authority_object(
        self,
        group: str,
        version: str,
        resource: str,
        namespace: str | None,
        name: str,
        *,
        expected_resource_version: int | None = None,
    ) -> bool:
        ns_key = str(namespace or "")
        deleted = False
        with self._connect() as conn:
            if expected_resource_version is not None:
                row = conn.execute(
                    """
                    SELECT resource_version
                      FROM authority_objects
                     WHERE grp = ? AND ver = ? AND resource = ? AND namespace = ? AND name = ?
                    """,
                    (group, version, resource, ns_key, name),
                ).fetchone()
                current_rv = int(row[0] or 0) if row is not None else 0
                if current_rv != int(expected_resource_version):
                    raise RegistryConflictError(
                        self._authority_object_conflict_key(group, version, resource, namespace, name),
                        expected=int(expected_resource_version),
                        actual=current_rv,
                    )
            conn.execute(
                """
                DELETE FROM authority_objects
                 WHERE grp = ? AND ver = ? AND resource = ? AND namespace = ? AND name = ?
                """,
                (group, version, resource, ns_key, name),
            )
            deleted = bool(getattr(conn, "total_changes", 0))
            conn.commit()
        return deleted

    def _authority_object_from_row(self, row) -> AuthorityObjectEntry | None:
        try:
            metadata = json.loads(row[6] or "{}")
            if not isinstance(metadata, dict):
                metadata = {}
        except Exception:
            metadata = {}
        try:
            spec = json.loads(row[7] or "{}")
            if not isinstance(spec, dict):
                spec = {}
        except Exception:
            spec = {}
        try:
            status = json.loads(row[8] or "{}")
            if not isinstance(status, dict):
                status = {}
        except Exception:
            status = {}
        try:
            updated = datetime.fromisoformat(row[9]) if row[9] else datetime.now(timezone.utc)
        except Exception:
            updated = datetime.now(timezone.utc)
        return AuthorityObjectEntry(
            group=str(row[0] or ""),
            version=str(row[1] or ""),
            resource=str(row[2] or ""),
            namespace=(str(row[3]) if row[3] not in {None, ""} else None),
            name=str(row[4] or ""),
            kind=str(row[5] or ""),
            metadata=metadata,
            spec=spec,
            status=status,
            updated_at=updated,
            resource_version=int(row[10] or 0),
        )

    def upsert_workload_metrics_snapshot(
        self,
        app_name: str,
        *,
        controller_id: str,
        controller_epoch: int,
        collected_at: datetime,
        cpu_utilization: float | None,
        memory_utilization: float | None,
        memory_bytes: int,
        pod_count: int,
        node_count: int,
    ) -> None:
        updated_at = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO workload_metrics_snapshots (
                  app_name,
                  controller_id,
                  controller_epoch,
                  collected_at,
                  cpu_utilization,
                  memory_utilization,
                  memory_bytes,
                  pod_count,
                  node_count,
                  updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(app_name) DO UPDATE SET
                  controller_id = excluded.controller_id,
                  controller_epoch = excluded.controller_epoch,
                  collected_at = excluded.collected_at,
                  cpu_utilization = excluded.cpu_utilization,
                  memory_utilization = excluded.memory_utilization,
                  memory_bytes = excluded.memory_bytes,
                  pod_count = excluded.pod_count,
                  node_count = excluded.node_count,
                  updated_at = excluded.updated_at
                """,
                (
                    str(app_name),
                    str(controller_id),
                    int(controller_epoch),
                    collected_at.astimezone(timezone.utc).isoformat(),
                    float(cpu_utilization) if cpu_utilization is not None else None,
                    float(memory_utilization) if memory_utilization is not None else None,
                    int(memory_bytes),
                    int(pod_count),
                    int(node_count),
                    updated_at,
                ),
            )
            conn.commit()

    def get_workload_metrics_snapshot(self, app_name: str) -> WorkloadMetricsSnapshot | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT app_name, controller_id, controller_epoch, collected_at,
                       cpu_utilization, memory_utilization, memory_bytes,
                       pod_count, node_count, updated_at
                  FROM workload_metrics_snapshots
                 WHERE app_name = ?
                """,
                (str(app_name),),
            ).fetchone()
        return self._workload_metrics_snapshot_from_row(row)

    def list_workload_metrics_snapshots(self) -> list[WorkloadMetricsSnapshot]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT app_name, controller_id, controller_epoch, collected_at,
                       cpu_utilization, memory_utilization, memory_bytes,
                       pod_count, node_count, updated_at
                  FROM workload_metrics_snapshots
                 ORDER BY app_name
                """
            ).fetchall()
        out: list[WorkloadMetricsSnapshot] = []
        for row in rows:
            entry = self._workload_metrics_snapshot_from_row(row)
            if entry is not None:
                out.append(entry)
        return out

    def delete_workload_metrics_snapshot(self, app_name: str) -> bool:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM workload_metrics_snapshots WHERE app_name = ?",
                (str(app_name),),
            )
            deleted = bool(getattr(conn, "total_changes", 0))
            conn.commit()
        return deleted

    def _workload_metrics_snapshot_from_row(self, row) -> WorkloadMetricsSnapshot | None:
        if row is None:
            return None
        try:
            collected_at = (
                datetime.fromisoformat(row[3]) if row[3] else datetime.fromtimestamp(0, timezone.utc)
            )
        except Exception:
            collected_at = datetime.fromtimestamp(0, timezone.utc)
        try:
            updated_at = (
                datetime.fromisoformat(row[9]) if row[9] else datetime.fromtimestamp(0, timezone.utc)
            )
        except Exception:
            updated_at = datetime.fromtimestamp(0, timezone.utc)
        return WorkloadMetricsSnapshot(
            app_name=str(row[0] or ""),
            controller_id=str(row[1] or ""),
            controller_epoch=int(row[2] or 0),
            collected_at=collected_at,
            cpu_utilization=float(row[4]) if row[4] is not None else None,
            memory_utilization=float(row[5]) if row[5] is not None else None,
            memory_bytes=int(row[6] or 0),
            pod_count=int(row[7] or 0),
            node_count=int(row[8] or 0),
            updated_at=updated_at,
        )

    def _get_latest_revision(self, app_name: str) -> Optional[RevisionInfo]:
        with self._connect() as conn:
            row = conn.execute(
                resource_loader.load_text("sql", "controller", "select_latest_revision.sql"),
                (app_name,),
            ).fetchone()
        if row is None:
            return None
        try:
            created = datetime.fromisoformat(row[4]) if row[4] else None
        except Exception:
            created = None
        return RevisionInfo(
            revision=row[0],
            spec_hash=row[1],
            status=row[2],
            image=row[3],
            created_at=created,
        )

    def get_revision_manifest(self, app_name: str, revision: int) -> AppManifest:
        with self._connect() as conn:
            row = conn.execute(
                resource_loader.load_text("sql", "controller", "select_revision_spec_json.sql"),
                (app_name, revision),
            ).fetchone()
        if row is None:
            raise ValueError(f"No revision {revision} recorded for {app_name}")
        return AppManifest.model_validate_json(row[0])

    def list_revisions(self, app_name: str, limit: int = 10) -> list[RevisionInfo]:
        with self._connect() as conn:
            rows = conn.execute(
                resource_loader.load_text("sql", "controller", "select_revisions_by_app.sql"),
                (app_name, limit),
            ).fetchall()
        result: list[RevisionInfo] = []
        for row in rows:
            try:
                created = datetime.fromisoformat(row[4]) if row[4] else None
            except Exception:
                created = None
            result.append(
                RevisionInfo(
                    revision=row[0],
                    spec_hash=row[1],
                    status=row[2],
                    image=row[3],
                    created_at=created,
                )
            )
        return result

    def record_event(self, app_name: str, revision: int, event_type: str, message: str) -> None:
        created_at = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                resource_loader.load_text("sql", "controller", "insert_app_events.sql"),
                (app_name, revision, event_type, message, created_at),
            )
            conn.commit()

    def list_events(self, app_name: str, limit: int = 20) -> list[AppEvent]:
        with self._connect() as conn:
            rows = conn.execute(
                resource_loader.load_text("sql", "controller", "select_app_events_by_app.sql"),
                (app_name, limit),
            ).fetchall()
        events: list[AppEvent] = []
        for row in rows:
            created = datetime.fromisoformat(row[3])
            events.append(
                AppEvent(
                    app_name=app_name,
                    revision=row[0],
                    event_type=row[1],
                    message=row[2],
                    created_at=created,
                )
            )
        return events

    def list_events_paginated(
        self, app_name: str, limit: int, offset: int
    ) -> tuple[list[AppEvent], int]:
        with self._connect() as conn:
            total = conn.execute(
                "SELECT COUNT(*) FROM app_events WHERE app_name = ?",
                (app_name,),
            ).fetchone()[0]
            rows = conn.execute(
                resource_loader.load_text("sql", "controller", "select_app_events_paginated.sql"),
                (app_name, limit, offset),
            ).fetchall()
        events: list[AppEvent] = []
        for row in rows:
            created = datetime.fromisoformat(row[3])
            events.append(
                AppEvent(
                    app_name=app_name,
                    revision=row[0],
                    event_type=row[1],
                    message=row[2],
                    created_at=created,
                )
            )
        return events, total

    # --- Inference cells ---
    def _inference_key(self, name: str, namespace: str | None) -> str:
        ns = str(namespace or DEFAULT_NAMESPACE).strip() or DEFAULT_NAMESPACE
        return app_key(name, ns)

    def _parse_iso_datetime(self, value: str | None) -> datetime:
        if value:
            try:
                return datetime.fromisoformat(value)
            except Exception:
                pass
        return datetime.now(timezone.utc)

    def register_inference_cell(
        self,
        manifest: InferenceCellManifest,
        *,
        source: str | None = None,
    ) -> None:
        namespace = manifest.metadata.namespace or DEFAULT_NAMESPACE
        cell_id = manifest.metadata.name
        cell_key = self._inference_key(cell_id, namespace)
        spec_json = json.dumps(manifest.model_dump(by_alias=True), sort_keys=True)
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT phase, allocations_json, admission_json, conditions_json, restarts, last_error
                FROM inference_cells
                WHERE cell_key = ?
                """,
                (cell_key,),
            ).fetchone()
            if row is None:
                phase = "PENDING"
                allocations_json = "{}"
                admission_json = "{}"
                conditions_json = "{}"
                restarts = 0
                last_error = None
            else:
                phase = str(row[0] or "PENDING")
                allocations_json = str(row[1] or "{}")
                admission_json = str(row[2] or "{}")
                conditions_json = str(row[3] or "{}")
                restarts = int(row[4] or 0)
                last_error = row[5]
            conn.execute(
                """
                INSERT INTO inference_cells (
                  cell_key, namespace, cell_id, spec_json, model_id, tp, pp,
                  executor_type, ray_scope, phase, allocations_json, admission_json,
                  conditions_json, restarts, last_error, source, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(cell_key) DO UPDATE SET
                  namespace = excluded.namespace,
                  cell_id = excluded.cell_id,
                  spec_json = excluded.spec_json,
                  model_id = excluded.model_id,
                  tp = excluded.tp,
                  pp = excluded.pp,
                  executor_type = excluded.executor_type,
                  ray_scope = excluded.ray_scope,
                  phase = excluded.phase,
                  allocations_json = excluded.allocations_json,
                  admission_json = excluded.admission_json,
                  conditions_json = excluded.conditions_json,
                  restarts = excluded.restarts,
                  last_error = excluded.last_error,
                  source = excluded.source,
                  updated_at = excluded.updated_at
                """,
                (
                    cell_key,
                    namespace,
                    cell_id,
                    spec_json,
                    manifest.spec.model.model_id,
                    int(manifest.spec.parallelism.tp),
                    int(manifest.spec.parallelism.pp),
                    manifest.spec.executor.type,
                    manifest.spec.executor.ray_scope,
                    phase,
                    allocations_json,
                    admission_json,
                    conditions_json,
                    restarts,
                    last_error,
                    str(source or "unknown"),
                    now_iso,
                ),
            )
            conn.commit()

    def _cell_record_from_row(self, row) -> InferenceCellRecord | None:
        try:
            manifest = InferenceCellManifest.model_validate_json(row[3])
        except Exception:
            return None
        try:
            allocations = json.loads(row[9] or "{}")
            if not isinstance(allocations, dict):
                allocations = {}
        except Exception:
            allocations = {}
        try:
            admission = json.loads(row[10] or "{}")
            if not isinstance(admission, dict):
                admission = {}
        except Exception:
            admission = {}
        try:
            conditions = json.loads(row[11] or "{}")
            if not isinstance(conditions, dict):
                conditions = {}
        except Exception:
            conditions = {}
        return InferenceCellRecord(
            cell_key=row[0],
            namespace=row[1],
            cell_id=row[2],
            manifest=manifest,
            phase=row[8],
            tp=int(row[5]),
            pp=int(row[6]),
            executor_type=row[7],
            ray_scope=manifest.spec.executor.ray_scope,
            allocations=allocations,
            admission=admission,
            conditions=conditions,
            restarts=int(row[12] or 0),
            last_error=row[13],
            source=row[14],
            updated_at=self._parse_iso_datetime(row[15]),
        )

    def get_inference_cell(
        self,
        name: str,
        namespace: str | None = None,
    ) -> InferenceCellRecord | None:
        cell_key = self._inference_key(name, namespace)
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT cell_key, namespace, cell_id, spec_json, model_id, tp, pp,
                       executor_type, phase, allocations_json, admission_json, conditions_json,
                       restarts, last_error, source, updated_at
                FROM inference_cells
                WHERE cell_key = ?
                """,
                (cell_key,),
            ).fetchone()
        if row is None:
            return None
        return self._cell_record_from_row(row)

    def list_inference_cells(self, namespace: str | None = None) -> list[InferenceCellRecord]:
        rows = []
        with self._connect() as conn:
            if namespace:
                rows = conn.execute(
                    """
                    SELECT cell_key, namespace, cell_id, spec_json, model_id, tp, pp,
                           executor_type, phase, allocations_json, admission_json, conditions_json,
                           restarts, last_error, source, updated_at
                    FROM inference_cells
                    WHERE namespace = ?
                    ORDER BY cell_id
                    """,
                    (str(namespace or DEFAULT_NAMESPACE),),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT cell_key, namespace, cell_id, spec_json, model_id, tp, pp,
                           executor_type, phase, allocations_json, admission_json, conditions_json,
                           restarts, last_error, source, updated_at
                    FROM inference_cells
                    ORDER BY namespace, cell_id
                    """
                ).fetchall()
        records: list[InferenceCellRecord] = []
        for row in rows:
            rec = self._cell_record_from_row(row)
            if rec is not None:
                records.append(rec)
        return records

    def update_inference_cell_status(
        self,
        name: str,
        namespace: str | None = None,
        *,
        phase: str | None = None,
        allocations: dict | None = None,
        admission: dict | None = None,
        conditions: dict | None = None,
        restarts: int | None = None,
        last_error: str | None | object = _UNSET,
    ) -> bool:
        cell_key = self._inference_key(name, namespace)
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT phase, allocations_json, admission_json, conditions_json, restarts, last_error
                FROM inference_cells WHERE cell_key = ?
                """,
                (cell_key,),
            ).fetchone()
            if row is None:
                return False
            next_phase = phase or str(row[0] or "PENDING")
            next_alloc = json.dumps(
                allocations if allocations is not None else json.loads(row[1] or "{}"),
                sort_keys=True,
            )
            next_adm = json.dumps(
                admission if admission is not None else json.loads(row[2] or "{}"), sort_keys=True
            )
            next_cond = json.dumps(
                conditions if conditions is not None else json.loads(row[3] or "{}"), sort_keys=True
            )
            next_restarts = int(restarts if restarts is not None else int(row[4] or 0))
            if last_error is _UNSET:
                next_error = row[5]
            else:
                next_error = last_error
            conn.execute(
                """
                UPDATE inference_cells
                SET phase = ?, allocations_json = ?, admission_json = ?, conditions_json = ?,
                    restarts = ?, last_error = ?, updated_at = ?
                WHERE cell_key = ?
                """,
                (
                    next_phase,
                    next_alloc,
                    next_adm,
                    next_cond,
                    next_restarts,
                    next_error,
                    now_iso,
                    cell_key,
                ),
            )
            conn.commit()
        return True

    def delete_inference_cell(self, name: str, namespace: str | None = None) -> None:
        cell_key = self._inference_key(name, namespace)
        with self._connect() as conn:
            conn.execute("DELETE FROM inference_cells WHERE cell_key = ?", (cell_key,))
            conn.execute("DELETE FROM inference_cell_events WHERE cell_key = ?", (cell_key,))
            conn.execute("DELETE FROM inference_fabric_sessions WHERE cell_key = ?", (cell_key,))
            conn.execute("DELETE FROM inference_gpu_leases WHERE cell_key = ?", (cell_key,))
            conn.execute("DELETE FROM inference_port_leases WHERE cell_key = ?", (cell_key,))
            conn.execute("DELETE FROM inference_node_locks WHERE cell_key = ?", (cell_key,))
            conn.commit()

    def record_inference_cell_event(
        self, name: str, namespace: str | None, event_type: str, message: str
    ) -> None:
        cell_key = self._inference_key(name, namespace)
        created_at = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO inference_cell_events (cell_key, event_type, message, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (cell_key, event_type, message, created_at),
            )
            conn.commit()

    def list_inference_cell_events(
        self, name: str, namespace: str | None = None, limit: int = 20
    ) -> list[InferenceCellEvent]:
        cell_key = self._inference_key(name, namespace)
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT event_type, message, created_at
                FROM inference_cell_events
                WHERE cell_key = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (cell_key, int(limit)),
            ).fetchall()
        events: list[InferenceCellEvent] = []
        for row in rows:
            events.append(
                InferenceCellEvent(
                    cell_key=cell_key,
                    event_type=str(row[0]),
                    message=str(row[1]),
                    created_at=self._parse_iso_datetime(row[2]),
                )
            )
        return events

    def register_inference_cellset(
        self, manifest: InferenceCellSetManifest, *, source: str | None = None
    ) -> None:
        namespace = manifest.metadata.namespace or DEFAULT_NAMESPACE
        name = manifest.metadata.name
        set_key = self._inference_key(name, namespace)
        spec_json = json.dumps(manifest.model_dump(by_alias=True), sort_keys=True)
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT current, ready, last_error FROM inference_cell_sets WHERE set_key = ?",
                (set_key,),
            ).fetchone()
            current = int(row[0]) if row else 0
            ready = int(row[1]) if row else 0
            last_error = row[2] if row else None
            conn.execute(
                """
                INSERT INTO inference_cell_sets (
                  set_key, namespace, name, spec_json, desired, current, ready, last_error, source, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(set_key) DO UPDATE SET
                  namespace = excluded.namespace,
                  name = excluded.name,
                  spec_json = excluded.spec_json,
                  desired = excluded.desired,
                  current = excluded.current,
                  ready = excluded.ready,
                  last_error = excluded.last_error,
                  source = excluded.source,
                  updated_at = excluded.updated_at
                """,
                (
                    set_key,
                    namespace,
                    name,
                    spec_json,
                    int(manifest.spec.replicas),
                    current,
                    ready,
                    last_error,
                    str(source or "unknown"),
                    now_iso,
                ),
            )
            conn.commit()

    def _cellset_record_from_row(self, row) -> InferenceCellSetRecord | None:
        try:
            manifest = InferenceCellSetManifest.model_validate_json(row[3])
        except Exception:
            return None
        return InferenceCellSetRecord(
            set_key=row[0],
            namespace=row[1],
            name=row[2],
            manifest=manifest,
            desired=int(row[4]),
            current=int(row[5]),
            ready=int(row[6]),
            last_error=row[7],
            source=row[8],
            updated_at=self._parse_iso_datetime(row[9]),
        )

    def get_inference_cellset(
        self, name: str, namespace: str | None = None
    ) -> InferenceCellSetRecord | None:
        set_key = self._inference_key(name, namespace)
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT set_key, namespace, name, spec_json, desired, current, ready,
                       last_error, source, updated_at
                FROM inference_cell_sets
                WHERE set_key = ?
                """,
                (set_key,),
            ).fetchone()
        if row is None:
            return None
        return self._cellset_record_from_row(row)

    def list_inference_cellsets(self, namespace: str | None = None) -> list[InferenceCellSetRecord]:
        with self._connect() as conn:
            if namespace:
                rows = conn.execute(
                    """
                    SELECT set_key, namespace, name, spec_json, desired, current, ready,
                           last_error, source, updated_at
                    FROM inference_cell_sets
                    WHERE namespace = ?
                    ORDER BY name
                    """,
                    (str(namespace or DEFAULT_NAMESPACE),),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT set_key, namespace, name, spec_json, desired, current, ready,
                           last_error, source, updated_at
                    FROM inference_cell_sets
                    ORDER BY namespace, name
                    """
                ).fetchall()
        items: list[InferenceCellSetRecord] = []
        for row in rows:
            rec = self._cellset_record_from_row(row)
            if rec is not None:
                items.append(rec)
        return items

    def update_inference_cellset_status(
        self,
        name: str,
        namespace: str | None = None,
        *,
        desired: int | None = None,
        current: int | None = None,
        ready: int | None = None,
        last_error: str | None | object = _UNSET,
    ) -> bool:
        set_key = self._inference_key(name, namespace)
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT desired, current, ready, last_error FROM inference_cell_sets WHERE set_key = ?",
                (set_key,),
            ).fetchone()
            if row is None:
                return False
            next_desired = int(desired if desired is not None else row[0])
            next_current = int(current if current is not None else row[1])
            next_ready = int(ready if ready is not None else row[2])
            next_error = row[3] if last_error is _UNSET else last_error
            conn.execute(
                """
                UPDATE inference_cell_sets
                SET desired = ?, current = ?, ready = ?, last_error = ?, updated_at = ?
                WHERE set_key = ?
                """,
                (next_desired, next_current, next_ready, next_error, now_iso, set_key),
            )
            conn.commit()
        return True

    def delete_inference_cellset(self, name: str, namespace: str | None = None) -> None:
        set_key = self._inference_key(name, namespace)
        with self._connect() as conn:
            conn.execute("DELETE FROM inference_cell_sets WHERE set_key = ?", (set_key,))
            conn.commit()

    def upsert_fabric_session(
        self,
        *,
        session_id: str,
        cell_key: str,
        policy_mode: str,
        members: list[dict],
        allowed_rules: list[dict],
        status: str,
        expires_at: datetime | None,
    ) -> None:
        now_iso = datetime.now(timezone.utc).isoformat()
        exp_iso = expires_at.isoformat() if expires_at else None
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO inference_fabric_sessions (
                  session_id, cell_key, policy_mode, members_json, allowed_rules_json,
                  status, expires_at, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                  cell_key = excluded.cell_key,
                  policy_mode = excluded.policy_mode,
                  members_json = excluded.members_json,
                  allowed_rules_json = excluded.allowed_rules_json,
                  status = excluded.status,
                  expires_at = excluded.expires_at,
                  updated_at = excluded.updated_at
                """,
                (
                    session_id,
                    cell_key,
                    policy_mode,
                    json.dumps(members, sort_keys=True),
                    json.dumps(allowed_rules, sort_keys=True),
                    status,
                    exp_iso,
                    now_iso,
                    now_iso,
                ),
            )
            conn.commit()

    def delete_fabric_session(self, session_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM inference_fabric_sessions WHERE session_id = ?", (session_id,)
            )
            conn.commit()

    def list_fabric_sessions(
        self, cell_name: str | None = None, namespace: str | None = None
    ) -> list[FabricSessionRecord]:
        rows = []
        with self._connect() as conn:
            if cell_name:
                cell_key = self._inference_key(cell_name, namespace)
                rows = conn.execute(
                    """
                    SELECT session_id, cell_key, policy_mode, members_json, allowed_rules_json,
                           status, expires_at, created_at, updated_at
                    FROM inference_fabric_sessions
                    WHERE cell_key = ?
                    ORDER BY created_at DESC
                    """,
                    (cell_key,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT session_id, cell_key, policy_mode, members_json, allowed_rules_json,
                           status, expires_at, created_at, updated_at
                    FROM inference_fabric_sessions
                    ORDER BY created_at DESC
                    """
                ).fetchall()
        out: list[FabricSessionRecord] = []
        for row in rows:
            try:
                members = json.loads(row[3] or "[]")
                if not isinstance(members, list):
                    members = []
            except Exception:
                members = []
            try:
                rules = json.loads(row[4] or "[]")
                if not isinstance(rules, list):
                    rules = []
            except Exception:
                rules = []
            out.append(
                FabricSessionRecord(
                    session_id=row[0],
                    cell_key=row[1],
                    policy_mode=row[2],
                    members=members,
                    allowed_rules=rules,
                    status=row[5],
                    expires_at=self._parse_iso_datetime(row[6]) if row[6] else None,
                    created_at=self._parse_iso_datetime(row[7]),
                    updated_at=self._parse_iso_datetime(row[8]),
                )
            )
        return out

    def upsert_ai_runtime_profile(
        self,
        profile: dict,
        admission: dict,
        *,
        workerbee_status: dict | None = None,
    ) -> AIRuntimeProfileRecord:
        run_id = str(profile.get("run_id") or "").strip()
        track = str(profile.get("track") or "").strip()
        if not run_id:
            raise ValueError("AI runtime profile run_id is required")
        if not track:
            raise ValueError("AI runtime profile track is required")
        warning_codes = self._ai_runtime_profile_warning_codes(admission)
        admitted = bool(admission.get("ok") and admission.get("admitted"))
        promotion_ready = admitted and not warning_codes
        now_iso = datetime.now(timezone.utc).isoformat()
        created_at_iso = now_iso
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT created_at FROM ai_runtime_profiles WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if existing is not None and existing[0]:
                created_at_iso = str(existing[0])
            conn.execute(
                """
                INSERT INTO ai_runtime_profiles (
                  run_id, track, profile_json, admission_json, workerbee_status_json,
                  warning_codes_json, admitted, promotion_ready, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                  track = excluded.track,
                  profile_json = excluded.profile_json,
                  admission_json = excluded.admission_json,
                  workerbee_status_json = excluded.workerbee_status_json,
                  warning_codes_json = excluded.warning_codes_json,
                  admitted = excluded.admitted,
                  promotion_ready = excluded.promotion_ready,
                  updated_at = excluded.updated_at
                """,
                (
                    run_id,
                    track,
                    json.dumps(profile, sort_keys=True),
                    json.dumps(admission, sort_keys=True),
                    json.dumps(workerbee_status, sort_keys=True)
                    if workerbee_status is not None
                    else None,
                    json.dumps(warning_codes, sort_keys=True),
                    1 if admitted else 0,
                    1 if promotion_ready else 0,
                    created_at_iso,
                    now_iso,
                ),
            )
            conn.commit()
        record = self.get_ai_runtime_profile(run_id)
        if record is None:
            raise RuntimeError(f"failed to read stored AI runtime profile {run_id}")
        return record

    def get_ai_runtime_profile(self, run_id: str) -> AIRuntimeProfileRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT run_id, track, profile_json, admission_json, workerbee_status_json,
                       warning_codes_json, admitted, promotion_ready, created_at, updated_at
                FROM ai_runtime_profiles
                WHERE run_id = ?
                """,
                (str(run_id),),
            ).fetchone()
        return self._ai_runtime_profile_from_row(row)

    def latest_ai_runtime_profile(self, track: str) -> AIRuntimeProfileRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT run_id, track, profile_json, admission_json, workerbee_status_json,
                       warning_codes_json, admitted, promotion_ready, created_at, updated_at
                FROM ai_runtime_profiles
                WHERE track = ?
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (str(track),),
            ).fetchone()
        return self._ai_runtime_profile_from_row(row)

    def list_ai_runtime_profiles(self, track: str | None = None) -> list[AIRuntimeProfileRecord]:
        with self._connect() as conn:
            if track:
                rows = conn.execute(
                    """
                    SELECT run_id, track, profile_json, admission_json, workerbee_status_json,
                           warning_codes_json, admitted, promotion_ready, created_at, updated_at
                    FROM ai_runtime_profiles
                    WHERE track = ?
                    ORDER BY updated_at DESC
                    """,
                    (str(track),),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT run_id, track, profile_json, admission_json, workerbee_status_json,
                           warning_codes_json, admitted, promotion_ready, created_at, updated_at
                    FROM ai_runtime_profiles
                    ORDER BY updated_at DESC
                    """
                ).fetchall()
        return [
            item for row in rows if (item := self._ai_runtime_profile_from_row(row)) is not None
        ]

    def _ai_runtime_profile_from_row(self, row) -> AIRuntimeProfileRecord | None:
        if row is None:
            return None
        profile = self._json_dict(row[2])
        admission = self._json_dict(row[3])
        warning_codes = [str(item) for item in self._json_list(row[5])]
        return AIRuntimeProfileRecord(
            run_id=str(row[0]),
            track=str(row[1]),
            profile=profile,
            admission=admission,
            workerbee_status=self._json_dict(row[4]) if row[4] else None,
            warning_codes=warning_codes,
            admitted=bool(row[6]),
            promotion_ready=bool(row[7]),
            created_at=self._parse_iso_datetime(row[8]),
            updated_at=self._parse_iso_datetime(row[9]),
        )

    @staticmethod
    def _ai_runtime_profile_warning_codes(admission: dict) -> list[str]:
        codes: list[str] = []
        findings = admission.get("findings")
        if not isinstance(findings, list):
            return codes
        for finding in findings:
            if not isinstance(finding, dict) or finding.get("level") != "warning":
                continue
            code = str(finding.get("code") or "").strip()
            if code and code not in codes:
                codes.append(code)
        return codes

    @staticmethod
    def _json_dict(raw: str | None) -> dict:
        if not raw:
            return {}
        try:
            value = json.loads(raw)
        except Exception:
            return {}
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _json_list(raw: str | None) -> list:
        if not raw:
            return []
        try:
            value = json.loads(raw)
        except Exception:
            return []
        return value if isinstance(value, list) else []

    def upsert_fabric_chunk(self, record: FabricChunkRecord) -> None:
        payload = chunk_record_payload(record)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO fabric_chunks (
                  chunk_id, namespace, name, digest, size_bytes, source_kind,
                  source_ref, labels_json, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(chunk_id) DO UPDATE SET
                  namespace = excluded.namespace,
                  name = excluded.name,
                  digest = excluded.digest,
                  size_bytes = excluded.size_bytes,
                  source_kind = excluded.source_kind,
                  source_ref = excluded.source_ref,
                  labels_json = excluded.labels_json,
                  updated_at = excluded.updated_at
                """,
                (
                    payload["chunk_id"],
                    payload["namespace"],
                    payload["name"],
                    payload["digest"],
                    int(payload["size_bytes"]),
                    payload["source_kind"],
                    payload["source_ref"],
                    json.dumps(payload["labels"], sort_keys=True),
                    payload["created_at"],
                    payload["updated_at"],
                ),
            )
            conn.commit()

    def _fabric_chunk_from_row(self, row) -> FabricChunkRecord | None:
        try:
            return chunk_record_from_payload(
                {
                    "chunk_id": row[0],
                    "namespace": row[1],
                    "name": row[2],
                    "digest": row[3],
                    "size_bytes": row[4],
                    "source_kind": row[5],
                    "source_ref": row[6],
                    "labels": self._json_dict(row[7]),
                    "created_at": row[8],
                    "updated_at": row[9],
                }
            )
        except ValueError:
            return None

    def get_fabric_chunk(self, chunk_id: str) -> FabricChunkRecord | None:
        try:
            normalized = normalize_chunk_id(chunk_id)
        except ValueError:
            return None
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT chunk_id, namespace, name, digest, size_bytes, source_kind,
                       source_ref, labels_json, created_at, updated_at
                FROM fabric_chunks
                WHERE chunk_id = ?
                """,
                (normalized,),
            ).fetchone()
        if row is None:
            return None
        return self._fabric_chunk_from_row(row)

    def list_fabric_chunks(self, namespace: str | None = None) -> list[FabricChunkRecord]:
        with self._connect() as conn:
            if namespace:
                rows = conn.execute(
                    """
                    SELECT chunk_id, namespace, name, digest, size_bytes, source_kind,
                           source_ref, labels_json, created_at, updated_at
                    FROM fabric_chunks
                    WHERE namespace = ?
                    ORDER BY name, chunk_id
                    """,
                    (str(namespace),),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT chunk_id, namespace, name, digest, size_bytes, source_kind,
                           source_ref, labels_json, created_at, updated_at
                    FROM fabric_chunks
                    ORDER BY namespace, name, chunk_id
                    """
                ).fetchall()
        return [item for row in rows if (item := self._fabric_chunk_from_row(row)) is not None]

    def upsert_fabric_residency(self, record: FabricResidencyRecord) -> None:
        payload = residency_record_payload(record)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO fabric_residencies (
                  chunk_id, node_id, storage_device_id, path, state, integrity_state,
                  epoch, digest, verified_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(chunk_id, node_id) DO UPDATE SET
                  storage_device_id = excluded.storage_device_id,
                  path = excluded.path,
                  state = excluded.state,
                  integrity_state = excluded.integrity_state,
                  epoch = excluded.epoch,
                  digest = excluded.digest,
                  verified_at = excluded.verified_at,
                  updated_at = excluded.updated_at
                """,
                (
                    payload["chunk_id"],
                    payload["node_id"],
                    payload["storage_device_id"],
                    payload["path"],
                    payload["state"],
                    payload["integrity_state"],
                    int(payload["epoch"]),
                    payload["digest"],
                    payload["verified_at"],
                    payload["updated_at"],
                ),
            )
            conn.commit()

    def _fabric_residency_from_row(self, row) -> FabricResidencyRecord | None:
        try:
            return residency_record_from_payload(
                {
                    "chunk_id": row[0],
                    "node_id": row[1],
                    "storage_device_id": row[2],
                    "path": row[3],
                    "state": row[4],
                    "integrity_state": row[5],
                    "epoch": row[6],
                    "digest": row[7],
                    "verified_at": row[8],
                    "updated_at": row[9],
                }
            )
        except ValueError:
            return None

    def list_fabric_residencies(
        self,
        *,
        chunk_id: str | None = None,
        node_id: str | None = None,
    ) -> list[FabricResidencyRecord]:
        clauses: list[str] = []
        params: list[str] = []
        if chunk_id:
            try:
                params.append(normalize_chunk_id(chunk_id))
            except ValueError:
                return []
            clauses.append("chunk_id = ?")
        if node_id:
            params.append(str(node_id))
            clauses.append("node_id = ?")
        sql = """
            SELECT chunk_id, node_id, storage_device_id, path, state, integrity_state,
                   epoch, digest, verified_at, updated_at
            FROM fabric_residencies
        """
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY node_id, chunk_id"
        with self._connect() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [
            item for row in rows if (item := self._fabric_residency_from_row(row)) is not None
        ]

    def upsert_fabric_movement(self, record: FabricMovementRecord) -> None:
        payload = movement_record_payload(record)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO fabric_movements (
                  movement_id, chunk_id, direction, source_node_id, target_node_id,
                  status, requested_by, digest, epoch, created_at, updated_at,
                  started_at, finished_at, error
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(movement_id) DO UPDATE SET
                  chunk_id = excluded.chunk_id,
                  direction = excluded.direction,
                  source_node_id = excluded.source_node_id,
                  target_node_id = excluded.target_node_id,
                  status = excluded.status,
                  requested_by = excluded.requested_by,
                  digest = excluded.digest,
                  epoch = excluded.epoch,
                  updated_at = excluded.updated_at,
                  started_at = excluded.started_at,
                  finished_at = excluded.finished_at,
                  error = excluded.error
                """,
                (
                    payload["movement_id"],
                    payload["chunk_id"],
                    payload["direction"],
                    payload["source_node_id"],
                    payload["target_node_id"],
                    payload["status"],
                    payload["requested_by"],
                    payload["digest"],
                    int(payload["epoch"]),
                    payload["created_at"],
                    payload["updated_at"],
                    payload["started_at"],
                    payload["finished_at"],
                    payload["error"],
                ),
            )
            conn.commit()

    def _fabric_movement_from_row(self, row) -> FabricMovementRecord | None:
        try:
            return movement_record_from_payload(
                {
                    "movement_id": row[0],
                    "chunk_id": row[1],
                    "direction": row[2],
                    "source_node_id": row[3],
                    "target_node_id": row[4],
                    "status": row[5],
                    "requested_by": row[6],
                    "digest": row[7],
                    "epoch": row[8],
                    "created_at": row[9],
                    "updated_at": row[10],
                    "started_at": row[11],
                    "finished_at": row[12],
                    "error": row[13],
                }
            )
        except ValueError:
            return None

    def list_fabric_movements(
        self,
        *,
        chunk_id: str | None = None,
        limit: int = 100,
    ) -> list[FabricMovementRecord]:
        clauses: list[str] = []
        params: list[object] = []
        if chunk_id:
            try:
                params.append(normalize_chunk_id(chunk_id))
            except ValueError:
                return []
            clauses.append("chunk_id = ?")
        sql = """
            SELECT movement_id, chunk_id, direction, source_node_id, target_node_id,
                   status, requested_by, digest, epoch, created_at, updated_at,
                   started_at, finished_at, error
            FROM fabric_movements
        """
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, int(limit)))
        with self._connect() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [item for row in rows if (item := self._fabric_movement_from_row(row)) is not None]

    def record_fabric_advisory_request(self, record: FabricAdvisoryRequestRecord) -> None:
        payload = advisory_request_payload(record)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO fabric_advisory_requests (
                  request_id, subject_type, subject_id, intent, facts_ref,
                  locality_snapshot_ref, max_candidates, time_budget_ms,
                  policy_mode, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(request_id) DO UPDATE SET
                  subject_type = excluded.subject_type,
                  subject_id = excluded.subject_id,
                  intent = excluded.intent,
                  facts_ref = excluded.facts_ref,
                  locality_snapshot_ref = excluded.locality_snapshot_ref,
                  max_candidates = excluded.max_candidates,
                  time_budget_ms = excluded.time_budget_ms,
                  policy_mode = excluded.policy_mode
                """,
                (
                    payload["request_id"],
                    payload["subject_type"],
                    payload["subject_id"],
                    payload["intent"],
                    payload["facts_ref"],
                    payload["locality_snapshot_ref"],
                    int(payload["max_candidates"]),
                    int(payload["time_budget_ms"]),
                    payload["policy_mode"],
                    payload["created_at"],
                ),
            )
            conn.commit()

    def _fabric_advisory_request_from_row(self, row) -> FabricAdvisoryRequestRecord:
        return advisory_request_from_payload(
            {
                "request_id": row[0],
                "subject_type": row[1],
                "subject_id": row[2],
                "intent": row[3],
                "facts_ref": row[4],
                "locality_snapshot_ref": row[5],
                "max_candidates": row[6],
                "time_budget_ms": row[7],
                "policy_mode": row[8],
                "created_at": row[9],
            }
        )

    def list_fabric_advisory_requests(
        self,
        *,
        subject_type: str | None = None,
        subject_id: str | None = None,
        limit: int = 100,
    ) -> list[FabricAdvisoryRequestRecord]:
        clauses: list[str] = []
        params: list[object] = []
        if subject_type:
            clauses.append("subject_type = ?")
            params.append(str(subject_type))
        if subject_id:
            clauses.append("subject_id = ?")
            params.append(str(subject_id))
        sql = """
            SELECT request_id, subject_type, subject_id, intent, facts_ref,
                   locality_snapshot_ref, max_candidates, time_budget_ms,
                   policy_mode, created_at
            FROM fabric_advisory_requests
        """
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, int(limit)))
        with self._connect() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [self._fabric_advisory_request_from_row(row) for row in rows]

    def record_fabric_advisory_response(self, record: FabricAdvisoryResponseRecord) -> None:
        payload = advisory_response_payload(record)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO fabric_advisory_responses (
                  request_id, provider, status, recommendation, confidence,
                  evidence_refs_json, authoritative, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(request_id) DO UPDATE SET
                  provider = excluded.provider,
                  status = excluded.status,
                  recommendation = excluded.recommendation,
                  confidence = excluded.confidence,
                  evidence_refs_json = excluded.evidence_refs_json,
                  authoritative = excluded.authoritative
                """,
                (
                    payload["request_id"],
                    payload["provider"],
                    payload["status"],
                    payload["recommendation"],
                    payload["confidence"],
                    json.dumps(payload["evidence_refs"], sort_keys=True),
                    1 if payload["authoritative"] else 0,
                    payload["created_at"],
                ),
            )
            conn.commit()

    def _fabric_advisory_response_from_row(self, row) -> FabricAdvisoryResponseRecord:
        return advisory_response_from_payload(
            {
                "request_id": row[0],
                "provider": row[1],
                "status": row[2],
                "recommendation": row[3],
                "confidence": row[4],
                "evidence_refs": self._json_list(row[5]),
                "authoritative": bool(row[6]),
                "created_at": row[7],
            }
        )

    def list_fabric_advisory_responses(
        self,
        *,
        request_id: str | None = None,
        limit: int = 100,
    ) -> list[FabricAdvisoryResponseRecord]:
        params: list[object] = []
        sql = """
            SELECT request_id, provider, status, recommendation, confidence,
                   evidence_refs_json, authoritative, created_at
            FROM fabric_advisory_responses
        """
        if request_id:
            sql += " WHERE request_id = ?"
            params.append(str(request_id))
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, int(limit)))
        with self._connect() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [self._fabric_advisory_response_from_row(row) for row in rows]

    def record_fabric_decision_trace(self, record: FabricDecisionTraceRecord) -> None:
        payload = decision_trace_payload(record)
        accepted = payload["accepted"]
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO fabric_decision_traces (
                  trace_id, request_id, deterministic_baseline_json,
                  advisory_response_json, accepted, divergence_reason,
                  replay_status, continuity_signals_json, coherence_signals_json,
                  created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(trace_id) DO UPDATE SET
                  request_id = excluded.request_id,
                  deterministic_baseline_json = excluded.deterministic_baseline_json,
                  advisory_response_json = excluded.advisory_response_json,
                  accepted = excluded.accepted,
                  divergence_reason = excluded.divergence_reason,
                  replay_status = excluded.replay_status,
                  continuity_signals_json = excluded.continuity_signals_json,
                  coherence_signals_json = excluded.coherence_signals_json
                """,
                (
                    payload["trace_id"],
                    payload["request_id"],
                    json.dumps(payload["deterministic_baseline"], sort_keys=True),
                    json.dumps(payload["advisory_response"], sort_keys=True),
                    None if accepted is None else (1 if accepted else 0),
                    payload["divergence_reason"],
                    payload["replay_status"],
                    json.dumps(payload["continuity_signals"], sort_keys=True),
                    json.dumps(payload["coherence_signals"], sort_keys=True),
                    payload["created_at"],
                ),
            )
            conn.commit()

    def _fabric_decision_trace_from_row(self, row) -> FabricDecisionTraceRecord:
        accepted_raw = row[4]
        accepted = None if accepted_raw is None else bool(accepted_raw)
        return decision_trace_from_payload(
            {
                "trace_id": row[0],
                "request_id": row[1],
                "deterministic_baseline": self._json_dict(row[2]),
                "advisory_response": self._json_dict(row[3]),
                "accepted": accepted,
                "divergence_reason": row[5],
                "replay_status": row[6],
                "continuity_signals": self._json_dict(row[7]),
                "coherence_signals": self._json_dict(row[8]),
                "created_at": row[9],
            }
        )

    def list_fabric_decision_traces(
        self,
        *,
        request_id: str | None = None,
        limit: int = 100,
    ) -> list[FabricDecisionTraceRecord]:
        params: list[object] = []
        sql = """
            SELECT trace_id, request_id, deterministic_baseline_json,
                   advisory_response_json, accepted, divergence_reason,
                   replay_status, continuity_signals_json, coherence_signals_json,
                   created_at
            FROM fabric_decision_traces
        """
        if request_id:
            sql += " WHERE request_id = ?"
            params.append(str(request_id))
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, int(limit)))
        with self._connect() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [self._fabric_decision_trace_from_row(row) for row in rows]

    def upsert_fabric_transfer_capability(
        self, record: FabricTransferCapabilityRecord
    ) -> None:
        payload = transfer_capability_payload(record)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO fabric_transfer_capabilities (
                  capability_id, node_id, peer_node_id, transport, status, priority,
                  capabilities_json, fallback_transport, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(capability_id) DO UPDATE SET
                  node_id = excluded.node_id,
                  peer_node_id = excluded.peer_node_id,
                  transport = excluded.transport,
                  status = excluded.status,
                  priority = excluded.priority,
                  capabilities_json = excluded.capabilities_json,
                  fallback_transport = excluded.fallback_transport,
                  updated_at = excluded.updated_at
                """,
                (
                    payload["capability_id"],
                    payload["node_id"],
                    payload["peer_node_id"],
                    payload["transport"],
                    payload["status"],
                    int(payload["priority"]),
                    json.dumps(payload["capabilities"], sort_keys=True),
                    payload["fallback_transport"],
                    payload["created_at"],
                    payload["updated_at"],
                ),
            )
            conn.commit()

    def _fabric_transfer_capability_from_row(self, row) -> FabricTransferCapabilityRecord:
        return transfer_capability_from_payload(
            {
                "capability_id": row[0],
                "node_id": row[1],
                "peer_node_id": row[2],
                "transport": row[3],
                "status": row[4],
                "priority": row[5],
                "capabilities": self._json_dict(row[6]),
                "fallback_transport": row[7],
                "created_at": row[8],
                "updated_at": row[9],
            }
        )

    def list_fabric_transfer_capabilities(
        self,
        *,
        node_id: str | None = None,
        peer_node_id: str | None = None,
        transport: str | None = None,
        limit: int = 100,
    ) -> list[FabricTransferCapabilityRecord]:
        clauses: list[str] = []
        params: list[object] = []
        if node_id:
            clauses.append("node_id = ?")
            params.append(str(node_id))
        if peer_node_id:
            clauses.append("peer_node_id = ?")
            params.append(str(peer_node_id))
        if transport:
            clauses.append("transport = ?")
            params.append(str(transport))
        sql = """
            SELECT capability_id, node_id, peer_node_id, transport, status, priority,
                   capabilities_json, fallback_transport, created_at, updated_at
            FROM fabric_transfer_capabilities
        """
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY priority DESC, updated_at DESC LIMIT ?"
        params.append(max(1, int(limit)))
        with self._connect() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [self._fabric_transfer_capability_from_row(row) for row in rows]

    def upsert_fabric_transfer_lease(self, record: FabricTransferLeaseRecord) -> None:
        payload = transfer_lease_payload(record)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO fabric_transfer_leases (
                  lease_id, chunk_id, source_node_id, target_node_id, transport,
                  status, holder, landing_zone_id, digest, epoch, expires_at,
                  created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(lease_id) DO UPDATE SET
                  chunk_id = excluded.chunk_id,
                  source_node_id = excluded.source_node_id,
                  target_node_id = excluded.target_node_id,
                  transport = excluded.transport,
                  status = excluded.status,
                  holder = excluded.holder,
                  landing_zone_id = excluded.landing_zone_id,
                  digest = excluded.digest,
                  epoch = excluded.epoch,
                  expires_at = excluded.expires_at,
                  updated_at = excluded.updated_at
                """,
                (
                    payload["lease_id"],
                    payload["chunk_id"],
                    payload["source_node_id"],
                    payload["target_node_id"],
                    payload["transport"],
                    payload["status"],
                    payload["holder"],
                    payload["landing_zone_id"],
                    payload["digest"],
                    int(payload["epoch"]),
                    payload["expires_at"],
                    payload["created_at"],
                    payload["updated_at"],
                ),
            )
            conn.commit()

    def _fabric_transfer_lease_from_row(self, row) -> FabricTransferLeaseRecord | None:
        try:
            return transfer_lease_from_payload(
                {
                    "lease_id": row[0],
                    "chunk_id": row[1],
                    "source_node_id": row[2],
                    "target_node_id": row[3],
                    "transport": row[4],
                    "status": row[5],
                    "holder": row[6],
                    "landing_zone_id": row[7],
                    "digest": row[8],
                    "epoch": row[9],
                    "expires_at": row[10],
                    "created_at": row[11],
                    "updated_at": row[12],
                }
            )
        except ValueError:
            return None

    def list_fabric_transfer_leases(
        self,
        *,
        chunk_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[FabricTransferLeaseRecord]:
        clauses: list[str] = []
        params: list[object] = []
        if chunk_id:
            try:
                params.append(normalize_chunk_id(chunk_id))
            except ValueError:
                return []
            clauses.append("chunk_id = ?")
        if status:
            clauses.append("status = ?")
            params.append(str(status))
        sql = """
            SELECT lease_id, chunk_id, source_node_id, target_node_id, transport,
                   status, holder, landing_zone_id, digest, epoch, expires_at,
                   created_at, updated_at
            FROM fabric_transfer_leases
        """
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, int(limit)))
        with self._connect() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [
            item for row in rows if (item := self._fabric_transfer_lease_from_row(row)) is not None
        ]

    def upsert_fabric_landing_zone(self, record: FabricLandingZoneRecord) -> None:
        payload = landing_zone_payload(record)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO fabric_landing_zones (
                  zone_id, node_id, path, capacity_bytes, reserved_bytes,
                  safety_state, cleanup_policy, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(zone_id) DO UPDATE SET
                  node_id = excluded.node_id,
                  path = excluded.path,
                  capacity_bytes = excluded.capacity_bytes,
                  reserved_bytes = excluded.reserved_bytes,
                  safety_state = excluded.safety_state,
                  cleanup_policy = excluded.cleanup_policy,
                  updated_at = excluded.updated_at
                """,
                (
                    payload["zone_id"],
                    payload["node_id"],
                    payload["path"],
                    int(payload["capacity_bytes"]),
                    int(payload["reserved_bytes"]),
                    payload["safety_state"],
                    payload["cleanup_policy"],
                    payload["created_at"],
                    payload["updated_at"],
                ),
            )
            conn.commit()

    def _fabric_landing_zone_from_row(self, row) -> FabricLandingZoneRecord:
        return landing_zone_from_payload(
            {
                "zone_id": row[0],
                "node_id": row[1],
                "path": row[2],
                "capacity_bytes": row[3],
                "reserved_bytes": row[4],
                "safety_state": row[5],
                "cleanup_policy": row[6],
                "created_at": row[7],
                "updated_at": row[8],
            }
        )

    def list_fabric_landing_zones(
        self,
        *,
        node_id: str | None = None,
        limit: int = 100,
    ) -> list[FabricLandingZoneRecord]:
        params: list[object] = []
        sql = """
            SELECT zone_id, node_id, path, capacity_bytes, reserved_bytes,
                   safety_state, cleanup_policy, created_at, updated_at
            FROM fabric_landing_zones
        """
        if node_id:
            sql += " WHERE node_id = ?"
            params.append(str(node_id))
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(max(1, int(limit)))
        with self._connect() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [self._fabric_landing_zone_from_row(row) for row in rows]

    def record_fabric_transport_attempt(self, record: FabricTransportAttemptRecord) -> None:
        payload = transport_attempt_payload(record)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO fabric_transport_attempts (
                  attempt_id, lease_id, chunk_id, transport, status, fallback_used,
                  fallback_transport, error, started_at, finished_at, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(attempt_id) DO UPDATE SET
                  lease_id = excluded.lease_id,
                  chunk_id = excluded.chunk_id,
                  transport = excluded.transport,
                  status = excluded.status,
                  fallback_used = excluded.fallback_used,
                  fallback_transport = excluded.fallback_transport,
                  error = excluded.error,
                  started_at = excluded.started_at,
                  finished_at = excluded.finished_at
                """,
                (
                    payload["attempt_id"],
                    payload["lease_id"],
                    payload["chunk_id"],
                    payload["transport"],
                    payload["status"],
                    1 if payload["fallback_used"] else 0,
                    payload["fallback_transport"],
                    payload["error"],
                    payload["started_at"],
                    payload["finished_at"],
                    payload["created_at"],
                ),
            )
            conn.commit()

    def _fabric_transport_attempt_from_row(self, row) -> FabricTransportAttemptRecord | None:
        try:
            return transport_attempt_from_payload(
                {
                    "attempt_id": row[0],
                    "lease_id": row[1],
                    "chunk_id": row[2],
                    "transport": row[3],
                    "status": row[4],
                    "fallback_used": bool(row[5]),
                    "fallback_transport": row[6],
                    "error": row[7],
                    "started_at": row[8],
                    "finished_at": row[9],
                    "created_at": row[10],
                }
            )
        except ValueError:
            return None

    def list_fabric_transport_attempts(
        self,
        *,
        lease_id: str | None = None,
        chunk_id: str | None = None,
        limit: int = 100,
    ) -> list[FabricTransportAttemptRecord]:
        clauses: list[str] = []
        params: list[object] = []
        if lease_id:
            clauses.append("lease_id = ?")
            params.append(str(lease_id))
        if chunk_id:
            try:
                params.append(normalize_chunk_id(chunk_id))
            except ValueError:
                return []
            clauses.append("chunk_id = ?")
        sql = """
            SELECT attempt_id, lease_id, chunk_id, transport, status, fallback_used,
                   fallback_transport, error, started_at, finished_at, created_at
            FROM fabric_transport_attempts
        """
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, int(limit)))
        with self._connect() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [
            item
            for row in rows
            if (item := self._fabric_transport_attempt_from_row(row)) is not None
        ]

    def upsert_fabric_das_cell_bundle(self, record: FabricDasCellBundleRecord) -> None:
        payload = das_cell_bundle_payload(record)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO fabric_das_cell_bundles (
                  bundle_id, site_id, cell_id, version, storage_ref, facts_ref,
                  status, labels_json, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(bundle_id) DO UPDATE SET
                  site_id = excluded.site_id,
                  cell_id = excluded.cell_id,
                  version = excluded.version,
                  storage_ref = excluded.storage_ref,
                  facts_ref = excluded.facts_ref,
                  status = excluded.status,
                  labels_json = excluded.labels_json,
                  updated_at = excluded.updated_at
                """,
                (
                    payload["bundle_id"],
                    payload["site_id"],
                    payload["cell_id"],
                    payload["version"],
                    payload["storage_ref"],
                    payload["facts_ref"],
                    payload["status"],
                    json.dumps(payload["labels"], sort_keys=True),
                    payload["created_at"],
                    payload["updated_at"],
                ),
            )
            conn.commit()

    def _fabric_das_cell_bundle_from_row(self, row) -> FabricDasCellBundleRecord:
        return das_cell_bundle_from_payload(
            {
                "bundle_id": row[0],
                "site_id": row[1],
                "cell_id": row[2],
                "version": row[3],
                "storage_ref": row[4],
                "facts_ref": row[5],
                "status": row[6],
                "labels": self._json_dict(row[7]),
                "created_at": row[8],
                "updated_at": row[9],
            }
        )

    def list_fabric_das_cell_bundles(
        self,
        *,
        site_id: str | None = None,
        limit: int = 100,
    ) -> list[FabricDasCellBundleRecord]:
        params: list[object] = []
        sql = """
            SELECT bundle_id, site_id, cell_id, version, storage_ref, facts_ref,
                   status, labels_json, created_at, updated_at
            FROM fabric_das_cell_bundles
        """
        if site_id:
            sql += " WHERE site_id = ?"
            params.append(str(site_id))
        sql += " ORDER BY site_id, cell_id, version LIMIT ?"
        params.append(max(1, int(limit)))
        with self._connect() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [self._fabric_das_cell_bundle_from_row(row) for row in rows]

    def record_fabric_das_query_trace(self, record: FabricDasQueryTraceRecord) -> None:
        payload = das_query_trace_payload(record)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO fabric_das_query_traces (
                  trace_id, bundle_id, site_id, query_id, query_kind, local_first,
                  warmed_refs_json, promoted_refs_json, fallback_sites_json,
                  result_ref, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(trace_id) DO UPDATE SET
                  bundle_id = excluded.bundle_id,
                  site_id = excluded.site_id,
                  query_id = excluded.query_id,
                  query_kind = excluded.query_kind,
                  local_first = excluded.local_first,
                  warmed_refs_json = excluded.warmed_refs_json,
                  promoted_refs_json = excluded.promoted_refs_json,
                  fallback_sites_json = excluded.fallback_sites_json,
                  result_ref = excluded.result_ref
                """,
                (
                    payload["trace_id"],
                    payload["bundle_id"],
                    payload["site_id"],
                    payload["query_id"],
                    payload["query_kind"],
                    1 if payload["local_first"] else 0,
                    json.dumps(payload["warmed_refs"], sort_keys=True),
                    json.dumps(payload["promoted_refs"], sort_keys=True),
                    json.dumps(payload["fallback_sites"], sort_keys=True),
                    payload["result_ref"],
                    payload["created_at"],
                ),
            )
            conn.commit()

    def _fabric_das_query_trace_from_row(self, row) -> FabricDasQueryTraceRecord:
        return das_query_trace_from_payload(
            {
                "trace_id": row[0],
                "bundle_id": row[1],
                "site_id": row[2],
                "query_id": row[3],
                "query_kind": row[4],
                "local_first": bool(row[5]),
                "warmed_refs": self._json_list(row[6]),
                "promoted_refs": self._json_list(row[7]),
                "fallback_sites": self._json_list(row[8]),
                "result_ref": row[9],
                "created_at": row[10],
            }
        )

    def list_fabric_das_query_traces(
        self,
        *,
        bundle_id: str | None = None,
        site_id: str | None = None,
        limit: int = 100,
    ) -> list[FabricDasQueryTraceRecord]:
        clauses: list[str] = []
        params: list[object] = []
        if bundle_id:
            clauses.append("bundle_id = ?")
            params.append(str(bundle_id))
        if site_id:
            clauses.append("site_id = ?")
            params.append(str(site_id))
        sql = """
            SELECT trace_id, bundle_id, site_id, query_id, query_kind, local_first,
                   warmed_refs_json, promoted_refs_json, fallback_sites_json,
                   result_ref, created_at
            FROM fabric_das_query_traces
        """
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, int(limit)))
        with self._connect() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [self._fabric_das_query_trace_from_row(row) for row in rows]

    def record_fabric_das_replication(self, record: FabricDasReplicationRecord) -> None:
        payload = das_replication_payload(record)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO fabric_das_replications (
                  replication_id, bundle_id, source_site_id, target_site_id,
                  mode, status, approved_by, reason, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(replication_id) DO UPDATE SET
                  bundle_id = excluded.bundle_id,
                  source_site_id = excluded.source_site_id,
                  target_site_id = excluded.target_site_id,
                  mode = excluded.mode,
                  status = excluded.status,
                  approved_by = excluded.approved_by,
                  reason = excluded.reason,
                  updated_at = excluded.updated_at
                """,
                (
                    payload["replication_id"],
                    payload["bundle_id"],
                    payload["source_site_id"],
                    payload["target_site_id"],
                    payload["mode"],
                    payload["status"],
                    payload["approved_by"],
                    payload["reason"],
                    payload["created_at"],
                    payload["updated_at"],
                ),
            )
            conn.commit()

    def _fabric_das_replication_from_row(self, row) -> FabricDasReplicationRecord:
        return das_replication_from_payload(
            {
                "replication_id": row[0],
                "bundle_id": row[1],
                "source_site_id": row[2],
                "target_site_id": row[3],
                "mode": row[4],
                "status": row[5],
                "approved_by": row[6],
                "reason": row[7],
                "created_at": row[8],
                "updated_at": row[9],
            }
        )

    def list_fabric_das_replications(
        self,
        *,
        bundle_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[FabricDasReplicationRecord]:
        clauses: list[str] = []
        params: list[object] = []
        if bundle_id:
            clauses.append("bundle_id = ?")
            params.append(str(bundle_id))
        if status:
            clauses.append("status = ?")
            params.append(str(status))
        sql = """
            SELECT replication_id, bundle_id, source_site_id, target_site_id,
                   mode, status, approved_by, reason, created_at, updated_at
            FROM fabric_das_replications
        """
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, int(limit)))
        with self._connect() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [self._fabric_das_replication_from_row(row) for row in rows]

    def record_fabric_cognitive_signal(self, record: FabricCognitiveSignalRecord) -> None:
        payload = cognitive_signal_payload(record)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO fabric_cognitive_signals (
                  signal_id, subject_type, subject_id, signal_kind, continuity_ref,
                  coherence_score, overload_state, review_gate, advisory_trace_id,
                  created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(signal_id) DO UPDATE SET
                  subject_type = excluded.subject_type,
                  subject_id = excluded.subject_id,
                  signal_kind = excluded.signal_kind,
                  continuity_ref = excluded.continuity_ref,
                  coherence_score = excluded.coherence_score,
                  overload_state = excluded.overload_state,
                  review_gate = excluded.review_gate,
                  advisory_trace_id = excluded.advisory_trace_id
                """,
                (
                    payload["signal_id"],
                    payload["subject_type"],
                    payload["subject_id"],
                    payload["signal_kind"],
                    payload["continuity_ref"],
                    payload["coherence_score"],
                    payload["overload_state"],
                    payload["review_gate"],
                    payload["advisory_trace_id"],
                    payload["created_at"],
                ),
            )
            conn.commit()

    def _fabric_cognitive_signal_from_row(self, row) -> FabricCognitiveSignalRecord:
        return cognitive_signal_from_payload(
            {
                "signal_id": row[0],
                "subject_type": row[1],
                "subject_id": row[2],
                "signal_kind": row[3],
                "continuity_ref": row[4],
                "coherence_score": row[5],
                "overload_state": row[6],
                "review_gate": row[7],
                "advisory_trace_id": row[8],
                "created_at": row[9],
            }
        )

    def list_fabric_cognitive_signals(
        self,
        *,
        subject_type: str | None = None,
        subject_id: str | None = None,
        limit: int = 100,
    ) -> list[FabricCognitiveSignalRecord]:
        clauses: list[str] = []
        params: list[object] = []
        if subject_type:
            clauses.append("subject_type = ?")
            params.append(str(subject_type))
        if subject_id:
            clauses.append("subject_id = ?")
            params.append(str(subject_id))
        sql = """
            SELECT signal_id, subject_type, subject_id, signal_kind, continuity_ref,
                   coherence_score, overload_state, review_gate, advisory_trace_id,
                   created_at
            FROM fabric_cognitive_signals
        """
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, int(limit)))
        with self._connect() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [self._fabric_cognitive_signal_from_row(row) for row in rows]

    def acquire_inference_gpu_leases(
        self,
        *,
        lease_id: str,
        cell_key: str,
        slots: list[tuple[str, int]],
        ttl_seconds: int,
    ) -> bool:
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        exp_iso = (now + timedelta(seconds=max(1, int(ttl_seconds)))).isoformat()
        with self._connect() as conn:
            try:
                for node_id, gpu_idx in slots:
                    cur = conn.execute(
                        """
                        INSERT INTO inference_gpu_leases
                          (node_id, gpu_index, lease_id, cell_key, expires_at, created_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                        ON CONFLICT(node_id, gpu_index) DO NOTHING
                        """,
                        (node_id, int(gpu_idx), lease_id, cell_key, exp_iso, now_iso),
                    )
                    if int(getattr(cur, "rowcount", 0) or 0) == 0:
                        conn.rollback()
                        return False
                conn.commit()
                return True
            except Exception:
                conn.rollback()
                return False

    def release_inference_gpu_leases(self, lease_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM inference_gpu_leases WHERE lease_id = ?", (lease_id,))
            conn.commit()

    def reserve_inference_port(
        self,
        *,
        lease_id: str,
        cell_key: str,
        node_id: str,
        port: int,
        ttl_seconds: int,
    ) -> bool:
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        exp_iso = (now + timedelta(seconds=max(1, int(ttl_seconds)))).isoformat()
        with self._connect() as conn:
            try:
                cur = conn.execute(
                    """
                    INSERT INTO inference_port_leases
                      (node_id, port, lease_id, cell_key, expires_at, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(node_id, port) DO NOTHING
                    """,
                    (node_id, int(port), lease_id, cell_key, exp_iso, now_iso),
                )
                conn.commit()
                return int(getattr(cur, "rowcount", 0) or 0) > 0
            except Exception:
                conn.rollback()
                return False

    def reserve_inference_port_from_range(
        self,
        *,
        lease_id: str,
        cell_key: str,
        node_id: str,
        start: int,
        end: int,
        ttl_seconds: int,
    ) -> int | None:
        lo = int(min(start, end))
        hi = int(max(start, end))
        for port in range(lo, hi + 1):
            ok = self.reserve_inference_port(
                lease_id=lease_id,
                cell_key=cell_key,
                node_id=node_id,
                port=port,
                ttl_seconds=ttl_seconds,
            )
            if ok:
                return port
        return None

    def release_inference_port_leases(self, lease_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM inference_port_leases WHERE lease_id = ?", (lease_id,))
            conn.commit()

    def acquire_inference_node_locks(
        self,
        *,
        lease_id: str,
        cell_key: str,
        node_ids: list[str],
        ttl_seconds: int,
    ) -> bool:
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        exp_iso = (now + timedelta(seconds=max(1, int(ttl_seconds)))).isoformat()
        with self._connect() as conn:
            try:
                for node_id in node_ids:
                    cur = conn.execute(
                        """
                        INSERT INTO inference_node_locks
                          (node_id, lease_id, cell_key, expires_at, created_at)
                        VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(node_id) DO NOTHING
                        """,
                        (node_id, lease_id, cell_key, exp_iso, now_iso),
                    )
                    if int(getattr(cur, "rowcount", 0) or 0) == 0:
                        conn.rollback()
                        return False
                conn.commit()
                return True
            except Exception:
                conn.rollback()
                return False

    def release_inference_node_locks(self, lease_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM inference_node_locks WHERE lease_id = ?", (lease_id,))
            conn.commit()

    # --- Node leases (lab-edge) ---
    def acquire_lease(
        self,
        site_id: str,
        node_id: str,
        session_id: str,
        lease_ttl_ms: int,
        renew_after_ms: int,
        controller_epoch: int,
    ) -> NodeLease:
        now = datetime.now(timezone.utc)
        lease_id = str(uuid.uuid4())
        expires_at = now + timedelta(milliseconds=int(lease_ttl_ms))
        now_iso = now.isoformat()
        with self._connect() as conn:
            conn.execute("DELETE FROM node_leases WHERE node_id = ?", (node_id,))
            conn.execute(
                """
                INSERT INTO node_leases
                  (node_id, site_id, session_id, lease_id, controller_epoch,
                   lease_ttl_ms, renew_after_ms, last_renew_at, expires_at, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    node_id,
                    site_id,
                    session_id,
                    lease_id,
                    int(controller_epoch),
                    int(lease_ttl_ms),
                    int(renew_after_ms),
                    now_iso,
                    expires_at.isoformat(),
                    now_iso,
                    now_iso,
                ),
            )
            conn.commit()
        return NodeLease(
            node_id=node_id,
            site_id=site_id,
            session_id=session_id,
            lease_id=lease_id,
            controller_epoch=int(controller_epoch),
            lease_ttl_ms=int(lease_ttl_ms),
            renew_after_ms=int(renew_after_ms),
            last_renew_at=now,
            expires_at=expires_at,
        )

    def renew_lease(
        self,
        node_id: str,
        session_id: str,
        lease_id: str,
        *,
        now: datetime | None = None,
    ) -> tuple[NodeLease | None, str | None]:
        now_dt = now or datetime.now(timezone.utc)
        now_iso = now_dt.isoformat()
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT node_id, site_id, session_id, lease_id, controller_epoch,
                       lease_ttl_ms, renew_after_ms, last_renew_at, expires_at
                FROM node_leases WHERE node_id = ?
                """,
                (node_id,),
            ).fetchone()
            if row is None:
                return None, "unknown_lease"
            if str(row[2]) != str(session_id):
                return None, "invalid_session"
            if str(row[3]) != str(lease_id):
                return None, "unknown_lease"
            try:
                expires_at = datetime.fromisoformat(row[8])
            except Exception:
                expires_at = now_dt - timedelta(seconds=1)
            if expires_at <= now_dt:
                conn.execute("DELETE FROM node_leases WHERE node_id = ?", (node_id,))
                conn.commit()
                return None, "expired"
            lease_ttl_ms = int(row[5])
            renew_after_ms = int(row[6])
            new_expires = now_dt + timedelta(milliseconds=lease_ttl_ms)
            conn.execute(
                """
                UPDATE node_leases
                SET last_renew_at = ?, expires_at = ?, updated_at = ?
                WHERE node_id = ?
                """,
                (now_iso, new_expires.isoformat(), now_iso, node_id),
            )
            conn.commit()
            lease = NodeLease(
                node_id=str(row[0]),
                site_id=str(row[1]),
                session_id=str(row[2]),
                lease_id=str(row[3]),
                controller_epoch=int(row[4]),
                lease_ttl_ms=lease_ttl_ms,
                renew_after_ms=renew_after_ms,
                last_renew_at=now_dt,
                expires_at=new_expires,
            )
            return lease, None

    # --- Work queue (lab-edge) ---
    def enqueue_work(
        self,
        work_id: str,
        attempt: int,
        site_id: str,
        payload: dict,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        payload_json = json.dumps(payload)
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM work_queue WHERE work_id = ? AND attempt = ?",
                (work_id, attempt),
            )
            conn.execute(
                """
                INSERT INTO work_queue
                  (work_id, attempt, site_id, payload_json, state, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (work_id, attempt, site_id, payload_json, "Pending", now, now),
            )
            conn.commit()

    def pull_work(
        self,
        site_id: str,
        limit: int,
        visibility_timeout_ms: int,
    ) -> list[WorkQueueLease]:
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        timeout_ms = max(0, int(visibility_timeout_ms))
        lease_expires_at = now + timedelta(milliseconds=timeout_ms)
        exp_iso = lease_expires_at.isoformat()
        leases: list[WorkQueueLease] = []
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE work_queue
                SET state = ?, lease_id = NULL, leased_at = NULL,
                    lease_expires_at = NULL, acked_at = NULL, updated_at = ?
                WHERE state IN ('Leased', 'Acked')
                  AND lease_expires_at IS NOT NULL
                  AND lease_expires_at < ?
                """,
                ("Pending", now_iso, now_iso),
            )
            rows = conn.execute(
                """
                SELECT work_id, attempt, payload_json
                FROM work_queue
                WHERE site_id = ? AND state = 'Pending'
                ORDER BY created_at
                LIMIT ?
                """,
                (site_id, int(limit)),
            ).fetchall()
            for row in rows:
                work_id, attempt, payload_json = row[0], int(row[1]), row[2]
                lease_id = str(uuid.uuid4())
                cursor = conn.execute(
                    """
                    UPDATE work_queue
                    SET state = ?, lease_id = ?, leased_at = ?, lease_expires_at = ?, updated_at = ?
                    WHERE work_id = ? AND attempt = ? AND state = 'Pending'
                    """,
                    ("Leased", lease_id, now_iso, exp_iso, now_iso, work_id, attempt),
                )
                if getattr(cursor, "rowcount", 1) == 0:
                    continue
                payload = json.loads(payload_json) if payload_json else {}
                try:
                    self.update_work_state(work_id=work_id, attempt=attempt, state="Dispatched")
                except Exception:
                    pass
                leases.append(
                    WorkQueueLease(
                        work_id=work_id,
                        attempt=attempt,
                        site_id=site_id,
                        payload=payload,
                        lease_id=lease_id,
                        lease_expires_at=lease_expires_at,
                    )
                )
            conn.commit()
        return leases

    def ack_work(self, lease_ids: list[str]) -> int:
        if not lease_ids:
            return 0
        now = datetime.now(timezone.utc).isoformat()
        updated = 0
        with self._connect() as conn:
            for lease_id in lease_ids:
                cursor = conn.execute(
                    """
                    UPDATE work_queue
                    SET state = ?, acked_at = ?, updated_at = ?
                    WHERE lease_id = ?
                    """,
                    ("Acked", now, now, lease_id),
                )
                try:
                    updated += int(getattr(cursor, "rowcount", 0) or 0)
                except Exception:
                    pass
            conn.commit()
        return updated

    def ack_work_items(self, ack_items: list[dict]) -> int:
        if not ack_items:
            return 0
        now = datetime.now(timezone.utc).isoformat()
        updated = 0
        with self._connect() as conn:
            for item in ack_items:
                if not isinstance(item, dict):
                    continue
                lease_id = str(item.get("lease_id") or "").strip()
                if not lease_id:
                    continue
                envelope = parse_envelope(item)
                if envelope is None:
                    continue
                row = conn.execute(
                    """
                    SELECT work_id, attempt, payload_json
                    FROM work_queue
                    WHERE lease_id = ?
                    """,
                    (lease_id,),
                ).fetchone()
                if not row:
                    continue
                work_id = str(row[0] or "")
                attempt = int(row[1] or 0)
                try:
                    payload = json.loads(row[2]) if row[2] else {}
                except Exception:
                    payload = {}
                queued_envelope = parse_envelope(payload)
                if queued_envelope != envelope:
                    continue
                if item.get("work_id") not in {None, "", work_id}:
                    continue
                try:
                    ack_attempt = int(item.get("attempt") or 0)
                except Exception:
                    ack_attempt = 0
                if ack_attempt not in {0, attempt}:
                    continue
                cursor = conn.execute(
                    """
                    UPDATE work_queue
                    SET state = ?, acked_at = ?, updated_at = ?
                    WHERE lease_id = ?
                    """,
                    ("Acked", now, now, lease_id),
                )
                try:
                    updated += int(getattr(cursor, "rowcount", 0) or 0)
                except Exception:
                    pass
            conn.commit()
        return updated

    # --- Site ingress endpoints (edge ingress scaffolding) ---
    def get_site_ingress_endpoint(self, site_id: str) -> SiteIngressEndpoint | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT site_id, mode, core_proxy_port, public_urls_json,
                       quarantine_until, created_at, updated_at
                FROM site_ingress_endpoints
                WHERE site_id = ?
                """,
                (site_id,),
            ).fetchone()
            if not row:
                return None
            public_urls = json.loads(row[3]) if row[3] else []
            quarantine_until = None
            if row[4]:
                try:
                    quarantine_until = datetime.fromisoformat(row[4])
                except Exception:
                    quarantine_until = None
            return SiteIngressEndpoint(
                site_id=str(row[0]),
                mode=str(row[1]),
                core_proxy_port=int(row[2]) if row[2] is not None else None,
                public_urls=list(public_urls) if isinstance(public_urls, list) else [],
                quarantine_until=quarantine_until,
                created_at=datetime.fromisoformat(row[5]),
                updated_at=datetime.fromisoformat(row[6]),
            )

    def ensure_site_ingress_port(
        self,
        site_id: str,
        *,
        port_min: int = 18080,
        port_max: int = 18999,
        mode: str = "core-proxy",
    ) -> int:
        if port_min > port_max:
            raise ValueError("port_min must be <= port_max")
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT core_proxy_port FROM site_ingress_endpoints WHERE site_id = ?",
                (site_id,),
            ).fetchone()
            if row and row[0] is not None:
                return int(row[0])
            used = {
                int(r[0])
                for r in conn.execute(
                    """
                    SELECT core_proxy_port
                    FROM site_ingress_endpoints
                    WHERE core_proxy_port IS NOT NULL
                    """
                ).fetchall()
            }
            for port in range(int(port_min), int(port_max) + 1):
                if port in used:
                    continue
                try:
                    if row:
                        conn.execute(
                            """
                            UPDATE site_ingress_endpoints
                            SET mode = ?, core_proxy_port = ?, updated_at = ?
                            WHERE site_id = ?
                            """,
                            (mode, port, now_iso, site_id),
                        )
                    else:
                        conn.execute(
                            """
                            INSERT INTO site_ingress_endpoints
                              (site_id, mode, core_proxy_port, public_urls_json,
                               quarantine_until, created_at, updated_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?)
                            """,
                            (site_id, mode, port, json.dumps([]), None, now_iso, now_iso),
                        )
                    conn.commit()
                    return port
                except Exception:
                    # retry on constraint conflicts
                    continue
        raise RuntimeError("no core-proxy ports available")

    def list_site_ingress_endpoints(self) -> list[SiteIngressListItem]:
        items: list[SiteIngressListItem] = []
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT site_id, mode, core_proxy_port, public_urls_json, quarantine_until
                FROM site_ingress_endpoints
                ORDER BY site_id
                """
            ).fetchall()
        for row in rows:
            public_urls = json.loads(row[3]) if row[3] else []
            quarantine_until = None
            if row[4]:
                try:
                    quarantine_until = datetime.fromisoformat(row[4])
                except Exception:
                    quarantine_until = None
            items.append(
                SiteIngressListItem(
                    site_id=str(row[0]),
                    mode=str(row[1]),
                    core_proxy_port=int(row[2]) if row[2] is not None else None,
                    public_urls=list(public_urls) if isinstance(public_urls, list) else [],
                    quarantine_until=quarantine_until,
                )
            )
        return items

    def upsert_site_ingress_endpoint(
        self,
        *,
        site_id: str,
        mode: str,
        core_proxy_port: int | None = None,
        public_urls: list[str | dict] | None = None,
        quarantine_until: datetime | None = None,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        public_json = json.dumps(public_urls or [])
        quarantine_val = quarantine_until.isoformat() if quarantine_until else None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT site_id, core_proxy_port FROM site_ingress_endpoints WHERE site_id = ?",
                (site_id,),
            ).fetchone()
            if row:
                existing_port = row[1]
                port_val = core_proxy_port if core_proxy_port is not None else existing_port
                conn.execute(
                    """
                    UPDATE site_ingress_endpoints
                    SET mode = ?, core_proxy_port = ?, public_urls_json = ?,
                        quarantine_until = ?, updated_at = ?
                    WHERE site_id = ?
                    """,
                    (mode, port_val, public_json, quarantine_val, now, site_id),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO site_ingress_endpoints
                      (site_id, mode, core_proxy_port, public_urls_json,
                       quarantine_until, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (site_id, mode, core_proxy_port, public_json, quarantine_val, now, now),
                )
            conn.commit()

    # --- Edge ingress routes/policies (edge-local bundles) ---
    def upsert_edge_ingress_route(
        self,
        *,
        name: str,
        namespace: str,
        site_id: str,
        policy_name: str | None,
        policy_namespace: str | None,
        document: dict,
        status: dict | None = None,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        payload = json.dumps(document, sort_keys=True)
        status_json = json.dumps(status, sort_keys=True) if status is not None else None
        with self._connect() as conn:
            conn.execute(
                resource_loader.load_text(
                    "sql", "controller", "insert_edge_ingress_routes_upsert.sql"
                ),
                (
                    name,
                    namespace,
                    site_id,
                    policy_name,
                    policy_namespace,
                    payload,
                    status_json,
                    now,
                    now,
                ),
            )
            conn.commit()

    def upsert_edge_ingress_policy(
        self,
        *,
        name: str,
        namespace: str,
        document: dict,
        status: dict | None = None,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        payload = json.dumps(document, sort_keys=True)
        status_json = json.dumps(status, sort_keys=True) if status is not None else None
        with self._connect() as conn:
            conn.execute(
                resource_loader.load_text(
                    "sql", "controller", "insert_edge_ingress_policies_upsert.sql"
                ),
                (
                    name,
                    namespace,
                    payload,
                    status_json,
                    now,
                    now,
                ),
            )
            conn.commit()

    def update_edge_ingress_route_status(
        self,
        *,
        name: str,
        namespace: str,
        status: dict,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        status_json = json.dumps(status, sort_keys=True)
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE edge_ingress_routes
                SET status_json = ?, updated_at = ?
                WHERE name = ? AND namespace = ?
                """,
                (status_json, now, name, namespace),
            )
            conn.commit()

    def delete_edge_ingress_route(self, *, name: str, namespace: str) -> bool:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM edge_ingress_routes WHERE name = ? AND namespace = ?",
                (name, namespace),
            )
            deleted = bool(getattr(conn, "total_changes", 0))
            conn.commit()
        return deleted

    def update_edge_ingress_policy_status(
        self,
        *,
        name: str,
        namespace: str,
        status: dict,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        status_json = json.dumps(status, sort_keys=True)
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE edge_ingress_policies
                SET status_json = ?, updated_at = ?
                WHERE name = ? AND namespace = ?
                """,
                (status_json, now, name, namespace),
            )
            conn.commit()

    def list_edge_ingress_routes(self) -> list[EdgeIngressRouteRecord]:
        items: list[EdgeIngressRouteRecord] = []
        with self._connect() as conn:
            rows = conn.execute(
                resource_loader.load_text("sql", "controller", "select_edge_ingress_routes_all.sql")
            ).fetchall()
        for row in rows:
            spec = {}
            status = None
            if row[5]:
                try:
                    spec = json.loads(row[5])
                except Exception:
                    spec = {}
            if row[6]:
                try:
                    status = json.loads(row[6])
                except Exception:
                    status = None
            created_at = datetime.now(timezone.utc)
            updated_at = created_at
            try:
                created_at = datetime.fromisoformat(row[7])
            except Exception:
                created_at = datetime.now(timezone.utc)
            try:
                updated_at = datetime.fromisoformat(row[8])
            except Exception:
                updated_at = created_at
            items.append(
                EdgeIngressRouteRecord(
                    name=str(row[0]),
                    namespace=str(row[1]),
                    site_id=str(row[2]),
                    policy_name=str(row[3]) if row[3] is not None else None,
                    policy_namespace=str(row[4]) if row[4] is not None else None,
                    spec=spec if isinstance(spec, dict) else {},
                    status=status if isinstance(status, dict) else None,
                    created_at=created_at,
                    updated_at=updated_at,
                )
            )
        return items

    def get_edge_ingress_route(
        self, *, name: str, namespace: str | None = None
    ) -> EdgeIngressRouteRecord | None:
        ns = namespace or "default"
        for record in self.list_edge_ingress_routes():
            if record.name == name and record.namespace == ns:
                return record
        return None

    def list_edge_ingress_routes_for_site(self, site_id: str) -> list[EdgeIngressRouteRecord]:
        items: list[EdgeIngressRouteRecord] = []
        with self._connect() as conn:
            rows = conn.execute(
                resource_loader.load_text(
                    "sql", "controller", "select_edge_ingress_routes_by_site.sql"
                ),
                (site_id,),
            ).fetchall()
        for row in rows:
            spec = {}
            status = None
            if row[5]:
                try:
                    spec = json.loads(row[5])
                except Exception:
                    spec = {}
            if row[6]:
                try:
                    status = json.loads(row[6])
                except Exception:
                    status = None
            created_at = datetime.now(timezone.utc)
            updated_at = created_at
            try:
                created_at = datetime.fromisoformat(row[7])
            except Exception:
                created_at = datetime.now(timezone.utc)
            try:
                updated_at = datetime.fromisoformat(row[8])
            except Exception:
                updated_at = created_at
            items.append(
                EdgeIngressRouteRecord(
                    name=str(row[0]),
                    namespace=str(row[1]),
                    site_id=str(row[2]),
                    policy_name=str(row[3]) if row[3] is not None else None,
                    policy_namespace=str(row[4]) if row[4] is not None else None,
                    spec=spec if isinstance(spec, dict) else {},
                    status=status if isinstance(status, dict) else None,
                    created_at=created_at,
                    updated_at=updated_at,
                )
            )
        return items

    def get_edge_ingress_policy(
        self, *, name: str, namespace: str
    ) -> EdgeIngressPolicyRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                resource_loader.load_text(
                    "sql", "controller", "select_edge_ingress_policy_by_key.sql"
                ),
                (name, namespace),
            ).fetchone()
        if not row:
            return None
        spec = {}
        status = None
        if row[2]:
            try:
                spec = json.loads(row[2])
            except Exception:
                spec = {}
        if row[3]:
            try:
                status = json.loads(row[3])
            except Exception:
                status = None
        try:
            created_at = datetime.fromisoformat(row[4])
        except Exception:
            created_at = datetime.now(timezone.utc)
        try:
            updated_at = datetime.fromisoformat(row[5])
        except Exception:
            updated_at = created_at
        return EdgeIngressPolicyRecord(
            name=str(row[0]),
            namespace=str(row[1]),
            spec=spec if isinstance(spec, dict) else {},
            status=status if isinstance(status, dict) else None,
            created_at=created_at,
            updated_at=updated_at,
        )

    def list_edge_ingress_policies(self) -> list[EdgeIngressPolicyRecord]:
        items: list[EdgeIngressPolicyRecord] = []
        with self._connect() as conn:
            rows = conn.execute(
                resource_loader.load_text(
                    "sql", "controller", "select_edge_ingress_policies_all.sql"
                )
            ).fetchall()
        for row in rows:
            spec = {}
            status = None
            if row[2]:
                try:
                    spec = json.loads(row[2])
                except Exception:
                    spec = {}
            if row[3]:
                try:
                    status = json.loads(row[3])
                except Exception:
                    status = None
            try:
                created_at = datetime.fromisoformat(row[4])
            except Exception:
                created_at = datetime.now(timezone.utc)
            try:
                updated_at = datetime.fromisoformat(row[5])
            except Exception:
                updated_at = created_at
            items.append(
                EdgeIngressPolicyRecord(
                    name=str(row[0]),
                    namespace=str(row[1]),
                    spec=spec if isinstance(spec, dict) else {},
                    status=status if isinstance(status, dict) else None,
                    created_at=created_at,
                    updated_at=updated_at,
                )
            )
        return items

    def list_site_ids(self) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT site_id FROM node_leases ORDER BY site_id"
            ).fetchall()
        return [str(row[0]) for row in rows if row and row[0]]

    def list_route_bundle_site_ids(self) -> list[str]:
        """Return site IDs eligible for route bundle publish.

        Includes sites from active node leases plus any sites explicitly
        referenced by EdgeIngressRoute placement.
        """
        sites: set[str] = set()
        with self._connect() as conn:
            lease_rows = conn.execute(
                "SELECT DISTINCT site_id FROM node_leases ORDER BY site_id"
            ).fetchall()
            for row in lease_rows:
                if row and row[0]:
                    site = str(row[0]).strip()
                    if site:
                        sites.add(site)

            route_rows = conn.execute(
                "SELECT DISTINCT site_id FROM edge_ingress_routes WHERE site_id IS NOT NULL"
            ).fetchall()
            for row in route_rows:
                if row and row[0]:
                    site = str(row[0]).strip()
                    if site:
                        sites.add(site)
        return sorted(sites)

    def mark_work_done(self, work_id: str, attempt: int) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE work_queue
                SET state = ?, updated_at = ?, lease_id = NULL, lease_expires_at = NULL
                WHERE work_id = ? AND attempt = ?
                """,
                ("Done", now, work_id, attempt),
            )
            conn.commit()

    # --- Outbox (jetstream) ---
    def enqueue_work_outbox(
        self,
        work_id: str,
        attempt: int,
        site_id: str,
        payload: dict,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        payload_json = json.dumps(payload)
        publish_subject = _outbox_publish_subject(site_id)
        publish_msg_id = _outbox_publish_msg_id(work_id, attempt, payload)
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM work_outbox WHERE work_id = ? AND attempt = ?",
                (work_id, attempt),
            )
            conn.execute(
                """
                INSERT INTO work_outbox
                  (work_id, attempt, site_id, payload_json, publish_subject, publish_msg_id,
                   state, publish_attempts, last_publish_at, last_publish_error, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    work_id,
                    int(attempt),
                    site_id,
                    payload_json,
                    publish_subject,
                    publish_msg_id,
                    "Unpublished",
                    0,
                    None,
                    None,
                    now,
                    now,
                ),
            )
            conn.commit()

    def list_outbox_unpublished(self, limit: int = 100) -> list[WorkOutboxEntry]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT work_id, attempt, site_id, payload_json, publish_subject,
                       publish_msg_id, publish_attempts, last_publish_at, last_publish_error
                FROM work_outbox
                WHERE state = 'Unpublished'
                ORDER BY created_at
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        entries: list[WorkOutboxEntry] = []
        for row in rows:
            payload = json.loads(row[3]) if row[3] else {}
            entries.append(
                WorkOutboxEntry(
                    work_id=str(row[0]),
                    attempt=int(row[1]),
                    site_id=str(row[2]),
                    payload=payload,
                    publish_subject=str(row[4] or _outbox_publish_subject(str(row[2]))),
                    publish_msg_id=str(
                        row[5] or _outbox_publish_msg_id(str(row[0]), int(row[1]), payload)
                    ),
                    publish_attempts=int(row[6] or 0),
                    last_publish_at=(
                        datetime.fromisoformat(str(row[7])) if row[7] else None
                    ),
                    last_publish_error=str(row[8]) if row[8] else None,
                )
            )
        return entries

    def get_outbox_payload(self, work_id: str, attempt: int) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT payload_json
                FROM work_outbox
                WHERE work_id = ? AND attempt = ?
                """,
                (work_id, int(attempt)),
            ).fetchone()
            if not row:
                return None
            try:
                return json.loads(row[0]) if row[0] else {}
            except Exception:
                return {}

    def mark_outbox_published(self, work_id: str, attempt: int) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE work_outbox
                SET state = ?, publish_attempts = publish_attempts + 1,
                    last_publish_at = ?, last_publish_error = NULL, updated_at = ?
                WHERE work_id = ? AND attempt = ?
                """,
                ("Published", now, now, work_id, int(attempt)),
            )
            conn.commit()

    def record_outbox_publish_attempt(
        self,
        work_id: str,
        attempt: int,
        *,
        error: str | None = None,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE work_outbox
                SET publish_attempts = publish_attempts + 1, last_publish_at = ?,
                    last_publish_error = ?, updated_at = ?
                WHERE work_id = ? AND attempt = ?
                """,
                (now, error, now, work_id, int(attempt)),
            )
            conn.commit()

    # --- Work ledger (jetstream watchdog) ---
    def upsert_work_ledger(
        self,
        *,
        work_id: str,
        attempt: int,
        site_id: str,
        state: str,
        controller_id: str | None = None,
        controller_epoch: int | None = None,
        operation_id: str | None = None,
        desired_generation: int | None = None,
    ) -> None:
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT work_id FROM work_ledger WHERE work_id = ?",
                (work_id,),
            ).fetchone()
            if row:
                conn.execute(
                    """
                    UPDATE work_ledger
                    SET attempt = ?, site_id = ?, state = ?, controller_id = ?,
                        controller_epoch = ?, operation_id = ?, desired_generation = ?,
                        updated_at = ?, state_updated_at = ?
                    WHERE work_id = ?
                    """,
                    (
                        int(attempt),
                        site_id,
                        state,
                        controller_id,
                        controller_epoch,
                        operation_id,
                        desired_generation,
                        now_iso,
                        now_iso,
                        work_id,
                    ),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO work_ledger
                      (work_id, attempt, site_id, state, controller_id, controller_epoch,
                       operation_id, desired_generation,
                       assigned_node_id, observed_generation, result_json,
                       created_at, updated_at, state_updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        work_id,
                        int(attempt),
                        site_id,
                        state,
                        controller_id,
                        controller_epoch,
                        operation_id,
                        desired_generation,
                        None,
                        None,
                        None,
                        now_iso,
                        now_iso,
                        now_iso,
                    ),
                )
            conn.commit()

    def get_work_ledger(self, work_id: str) -> WorkLedgerEntry | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT work_id, attempt, site_id, state, controller_id,
                       controller_epoch, operation_id, desired_generation,
                       assigned_node_id, observed_generation, result_json,
                       created_at, updated_at, state_updated_at
                FROM work_ledger
                WHERE work_id = ?
                """,
                (work_id,),
            ).fetchone()
            if not row:
                return None
            result = None
            if row[10]:
                try:
                    result = json.loads(row[10])
                except Exception:
                    result = None
            return WorkLedgerEntry(
                work_id=str(row[0]),
                attempt=int(row[1]),
                site_id=str(row[2]),
                state=str(row[3]),
                controller_id=str(row[4]) if row[4] else None,
                controller_epoch=int(row[5]) if row[5] is not None else None,
                operation_id=str(row[6]) if row[6] else None,
                desired_generation=int(row[7]) if row[7] is not None else None,
                assigned_node_id=str(row[8]) if row[8] else None,
                observed_generation=int(row[9]) if row[9] is not None else None,
                result=result,
                created_at=datetime.fromisoformat(row[11]),
                updated_at=datetime.fromisoformat(row[12]),
                state_updated_at=datetime.fromisoformat(row[13]),
            )

    def update_work_state(
        self,
        *,
        work_id: str,
        attempt: int,
        state: str,
        assigned_node_id: str | None = None,
        observed_generation: int | None = None,
        result: dict | None = None,
    ) -> bool:
        now_iso = datetime.now(timezone.utc).isoformat()
        result_json = json.dumps(result) if result is not None else None
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE work_ledger
                SET state = ?, assigned_node_id = COALESCE(?, assigned_node_id),
                    observed_generation = COALESCE(?, observed_generation),
                    result_json = COALESCE(?, result_json),
                    updated_at = ?, state_updated_at = ?
                WHERE work_id = ? AND attempt = ?
                """,
                (
                    state,
                    assigned_node_id,
                    observed_generation,
                    result_json,
                    now_iso,
                    now_iso,
                    work_id,
                    int(attempt),
                ),
            )
            try:
                updated = int(getattr(cursor, "rowcount", 0) or 0)
            except Exception:
                updated = 0
            conn.commit()
            return updated > 0

    def list_work_state_before(self, state: str, cutoff: datetime) -> list[WorkLedgerEntry]:
        cutoff_iso = cutoff.isoformat()
        rows: list[WorkLedgerEntry] = []
        with self._connect() as conn:
            results = conn.execute(
                """
                SELECT work_id, attempt, site_id, state, controller_id,
                       controller_epoch, operation_id, desired_generation,
                       assigned_node_id, observed_generation, result_json,
                       created_at, updated_at, state_updated_at
                FROM work_ledger
                WHERE state = ? AND state_updated_at <= ?
                ORDER BY state_updated_at
                """,
                (state, cutoff_iso),
            ).fetchall()
            for row in results:
                result = None
                if row[10]:
                    try:
                        result = json.loads(row[10])
                    except Exception:
                        result = None
                rows.append(
                    WorkLedgerEntry(
                        work_id=str(row[0]),
                        attempt=int(row[1]),
                        site_id=str(row[2]),
                        state=str(row[3]),
                        controller_id=str(row[4]) if row[4] else None,
                        controller_epoch=int(row[5]) if row[5] is not None else None,
                        operation_id=str(row[6]) if row[6] else None,
                        desired_generation=int(row[7]) if row[7] is not None else None,
                        assigned_node_id=str(row[8]) if row[8] else None,
                        observed_generation=int(row[9]) if row[9] is not None else None,
                        result=result,
                        created_at=datetime.fromisoformat(row[11]),
                        updated_at=datetime.fromisoformat(row[12]),
                        state_updated_at=datetime.fromisoformat(row[13]),
                    )
                )
        return rows

    def reschedule_work(
        self,
        *,
        work_id: str,
        attempt: int,
        controller_id: str | None = None,
        controller_epoch: int | None = None,
    ) -> int | None:
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT site_id, payload_json
                FROM work_outbox
                WHERE work_id = ? AND attempt = ?
                """,
                (work_id, int(attempt)),
            ).fetchone()
            if not row:
                return None
            site_id = str(row[0])
            try:
                payload = json.loads(row[1]) if row[1] else {}
            except Exception:
                payload = {}
            new_attempt = int(attempt) + 1
            payload["attempt"] = new_attempt
            payload.setdefault("work_id", work_id)
            payload.setdefault("site_id", site_id)
            payload["created_at"] = now_iso
            if controller_id:
                payload["controller_id"] = controller_id
            if controller_epoch is not None:
                payload["controller_epoch"] = int(controller_epoch)
            if payload.get("controller_id") and payload.get("controller_epoch") is not None:
                payload["operation_id"] = work_operation(work_id, new_attempt)
            cursor = conn.execute(
                """
                UPDATE work_ledger
                SET attempt = ?, state = ?, controller_id = ?, controller_epoch = ?,
                    operation_id = ?, updated_at = ?, state_updated_at = ?
                WHERE work_id = ? AND attempt = ?
                """,
                (
                    new_attempt,
                    "Pending",
                    payload.get("controller_id"),
                    payload.get("controller_epoch"),
                    payload.get("operation_id"),
                    now_iso,
                    now_iso,
                    work_id,
                    int(attempt),
                ),
            )
            try:
                updated = int(getattr(cursor, "rowcount", 0) or 0)
            except Exception:
                updated = 0
            if updated <= 0:
                conn.commit()
                return None
            conn.execute(
                """
                INSERT INTO work_outbox
                  (work_id, attempt, site_id, payload_json, publish_subject, publish_msg_id,
                   state, publish_attempts, last_publish_at, last_publish_error, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    work_id,
                    new_attempt,
                    site_id,
                    json.dumps(payload),
                    _outbox_publish_subject(site_id),
                    _outbox_publish_msg_id(work_id, new_attempt, payload),
                    "Unpublished",
                    0,
                    None,
                    None,
                    now_iso,
                    now_iso,
                ),
            )
            conn.commit()
            return new_attempt
    # --- Canary rollout state ----------------------------------------------

    def get_canary_state(self, app_name: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                resource_loader.load_text("sql", "controller", "select_rollout_canary.sql"),
                (app_name,),
            ).fetchone()
        if row is None:
            return None
        return {
            "weight": float(row[0]),
            "next_step_at": str(row[1]),
            "step": float(row[2]),
            "max": float(row[3]),
            "updated_at": str(row[4]),
        }

    def upsert_canary_state(
        self, app_name: str, *, weight: float, next_step_at: str, step: float, max_weight: float
    ) -> None:
        updated_at = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                resource_loader.load_text("sql", "controller", "upsert_rollout_canary.sql"),
                (app_name, float(weight), next_step_at, float(step), float(max_weight), updated_at),
            )
            conn.commit()

    # --- Services / Service IPAM -------------------------------------------

    def upsert_service(self, app_name: str, cluster_ip: str, ports: dict) -> None:
        """Persist or update service metadata (ClusterIP + ports)."""
        created_at = datetime.now(timezone.utc).isoformat()
        ports_json = json.dumps(ports, sort_keys=True)
        with self._connect() as conn:
            conn.execute(
                resource_loader.load_text("sql", "controller", "upsert_services.sql"),
                (app_name, cluster_ip, ports_json, created_at),
            )
            conn.commit()

    def upsert_service_snapshot(
        self, app_name: str, cluster_ip: str, ports: dict, endpoints: list[ServiceEndpoint]
    ) -> None:
        """Persist service metadata and endpoints together."""
        created_at = datetime.now(timezone.utc).isoformat()
        ports_json = json.dumps(ports, sort_keys=True)
        with self._connect() as conn:
            conn.execute(
                resource_loader.load_text("sql", "controller", "upsert_services.sql"),
                (app_name, cluster_ip, ports_json, created_at),
            )
            conn.execute("DELETE FROM service_endpoints WHERE app_name = ?", (app_name,))
            rows = [
                (ep.app_name, ep.port, ep.ip, ep.target_port, int(ep.ready)) for ep in endpoints
            ]
            if rows:
                conn.executemany(
                    resource_loader.load_text("sql", "controller", "insert_service_endpoints.sql"),
                    rows,
                )
            conn.commit()

    def delete_service(self, app_name: str) -> None:
        """Remove service metadata and endpoints for an app."""
        with self._connect() as conn:
            conn.execute("DELETE FROM service_endpoints WHERE app_name = ?", (app_name,))
            conn.execute("DELETE FROM services WHERE app_name = ?", (app_name,))
            conn.commit()

    def get_service(self, app_name: str) -> ServiceRecord | None:
        """Return service metadata if present."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT cluster_ip, ports FROM services WHERE app_name = ?", (app_name,)
            ).fetchone()
        if row is None:
            return None
        try:
            ports = json.loads(row[1]) if row[1] else {}
        except json.JSONDecodeError:
            ports = {}
        return ServiceRecord(app_name=app_name, cluster_ip=row[0], ports=ports)

    def upsert_service_endpoints(self, app_name: str, endpoints: list[ServiceEndpoint]) -> None:
        """Replace endpoints for an app."""
        with self._connect() as conn:
            conn.execute("DELETE FROM service_endpoints WHERE app_name = ?", (app_name,))
            rows = [
                (ep.app_name, ep.port, ep.ip, ep.target_port, int(ep.ready)) for ep in endpoints
            ]
            if rows:
                conn.executemany(
                    resource_loader.load_text("sql", "controller", "insert_service_endpoints.sql"),
                    rows,
                )
            conn.commit()

    def list_service_endpoints(self, app_name: str) -> list[ServiceEndpoint]:
        with self._connect() as conn:
            rows = conn.execute(
                resource_loader.load_text(
                    "sql", "controller", "select_service_endpoints_by_app.sql"
                ),
                (app_name,),
            ).fetchall()
        return [
            ServiceEndpoint(
                app_name=row[0],
                port=int(row[1]),
                ip=row[2],
                target_port=int(row[3]),
                ready=bool(row[4]),
            )
            for row in rows
        ]

    # Compatibility helper for older callers/tests
    def record_service_endpoints(self, app_name: str, endpoints: list[ServiceEndpoint]) -> None:
        self.upsert_service_endpoints(app_name, endpoints)

    def list_services(self) -> list[ServiceListItem]:
        """List all services stored (cluster IP allocation helper)."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT app_name, cluster_ip FROM services ORDER BY app_name"
            ).fetchall()
        return [ServiceListItem(app_name=row[0], cluster_ip=row[1]) for row in rows]

    # --- Nodes / heartbeats ---------------------------------------------

    def upsert_node(
        self,
        node_id: str,
        *,
        name: str | None = None,
        labels: dict | None = None,
        capabilities: dict | None = None,
        taints: list | None = None,
        backend: str | None = None,
        endpoint: str | None = None,
        pod_cidr: str | None = None,
        wg_pubkey: str | None = None,
        rp_pubkey: str | None = None,
        cordoned: bool | None = None,
    ) -> None:
        if cordoned is None:
            cordoned = self._get_node_cordoned(node_id)
        normalized_capabilities = normalize_capabilities(capabilities)
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                resource_loader.load_text("sql", "controller", "upsert_nodes.sql"),
                (
                    node_id,
                    name,
                    json.dumps(labels or {}, sort_keys=True),
                    json.dumps(normalized_capabilities, sort_keys=True),
                    json.dumps(taints or [], sort_keys=True),
                    backend,
                    endpoint,
                    pod_cidr,
                    wg_pubkey,
                    rp_pubkey,
                    int(bool(cordoned)),
                    now,
                    now,
                ),
            )
            conn.commit()

    def record_heartbeat(self, node_id: str, status: str) -> None:
        seen = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                resource_loader.load_text("sql", "controller", "upsert_node_heartbeats.sql"),
                (node_id, status, seen),
            )
            conn.commit()

    def _get_node_cordoned(self, node_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT cordoned FROM nodes WHERE node_id = ?",
                (node_id,),
            ).fetchone()
        return bool(row[0]) if row is not None else False

    def cordon_node(self, node_id: str, cordoned: bool = True) -> bool:
        """Mark a node as (un)cordoned for scheduling purposes."""
        with self._connect() as conn:
            res = conn.execute(
                "UPDATE nodes SET cordoned = ? WHERE node_id = ?",
                (int(bool(cordoned)), node_id),
            )
            conn.commit()
            return res.rowcount > 0

    def list_nodes(self) -> list[tuple[NodeRecord, NodeStatus | None]]:
        with self._connect() as conn:
            rows = conn.execute(
                resource_loader.load_text("sql", "controller", "select_nodes_with_heartbeat.sql")
            ).fetchall()
        result: list[tuple[NodeRecord, NodeStatus | None]] = []
        for row in rows:
            labels = {}
            capabilities = {}
            taints = []
            try:
                labels = json.loads(row[2] or "{}")
            except Exception:
                labels = {}
            try:
                capabilities = json.loads(row[3] or "{}")
            except Exception:
                capabilities = {}
            capabilities = normalize_capabilities(capabilities)
            try:
                taints = json.loads(row[4] or "[]")
            except Exception:
                taints = []
            try:
                created = datetime.fromisoformat(row[10])
            except Exception:
                created = datetime.fromtimestamp(0, tz=timezone.utc)
            try:
                updated = datetime.fromisoformat(row[11])
            except Exception:
                updated = datetime.fromtimestamp(0, tz=timezone.utc)
            node = NodeRecord(
                node_id=row[0],
                name=row[1],
                labels=labels,
                capabilities=capabilities,
                taints=taints,
                backend=row[5],
                endpoint=row[6],
                pod_cidr=row[7],
                wg_pubkey=row[8],
                rp_pubkey=row[9],
                cordoned=bool(row[12]),
                created_at=created,
                updated_at=updated,
            )
            status = None
            if row[13] is not None:
                try:
                    seen_at = datetime.fromisoformat(row[14])
                except Exception:
                    seen_at = datetime.fromtimestamp(0, tz=timezone.utc)
                status = NodeStatus(node_id=row[0], status=row[13], seen_at=seen_at)
            result.append((node, status))
        return result

    def get_node(self, node_id: str) -> tuple[NodeRecord, NodeStatus | None] | None:
        with self._connect() as conn:
            row = conn.execute(
                resource_loader.load_text("sql", "controller", "select_node_by_id.sql"),
                (node_id,),
            ).fetchone()
        if row is None:
            return None
        labels = {}
        capabilities = {}
        taints = []
        try:
            labels = json.loads(row[2] or "{}")
        except Exception:
            labels = {}
        try:
            capabilities = json.loads(row[3] or "{}")
        except Exception:
            capabilities = {}
        capabilities = normalize_capabilities(capabilities)
        try:
            taints = json.loads(row[4] or "[]")
        except Exception:
            taints = []
        try:
            created = datetime.fromisoformat(row[10])
        except Exception:
            created = datetime.fromtimestamp(0, tz=timezone.utc)
        try:
            updated = datetime.fromisoformat(row[11])
        except Exception:
            updated = datetime.fromtimestamp(0, tz=timezone.utc)
        node = NodeRecord(
            node_id=row[0],
            name=row[1],
            labels=labels,
            capabilities=capabilities,
            taints=taints,
            backend=row[5],
            endpoint=row[6],
            pod_cidr=row[7],
            wg_pubkey=row[8],
            rp_pubkey=row[9],
            cordoned=bool(row[12]),
            created_at=created,
            updated_at=updated,
        )
        status = None
        if row[13] is not None:
            try:
                seen_at = datetime.fromisoformat(row[14])
            except Exception:
                seen_at = datetime.fromtimestamp(0, tz=timezone.utc)
            status = NodeStatus(node_id=row[0], status=row[13], seen_at=seen_at)
        return node, status

    def get_node_capabilities(self, node_id: str) -> dict:
        """Return normalized capability facts for one node."""
        rec = self.get_node(node_id)
        if rec is None:
            return {}
        node, _status = rec
        return normalize_capabilities(node.capabilities)

    def list_node_capabilities(self) -> dict[str, dict]:
        """Return normalized capability facts keyed by node id."""
        return {
            node.node_id: normalize_capabilities(node.capabilities)
            for node, _status in self.list_nodes()
        }

    def list_fabric_link_metrics(self) -> list[dict]:
        """Return controller-visible fabric link metric samples from node facts."""
        samples: list[dict] = []
        seen: set[tuple[str, str, str]] = set()
        for node, _status in self.list_nodes():
            for metric in link_metric_inventory(node.capabilities):
                item = dict(metric)
                raw_source = str(item.get("source") or "").strip()
                source = raw_source if raw_source and raw_source != "node-capability" else node.node_id
                item["source"] = source
                key = (
                    str(item.get("from_site") or ""),
                    str(item.get("to_site") or ""),
                    source,
                )
                if key in seen:
                    continue
                seen.add(key)
                samples.append(item)
        return samples

    # --- Volume attachments --------------------------------------------

    def upsert_volume_attachment(
        self, app_name: str, volume_name: str, node_id: str, retention: str | None = None
    ) -> None:
        """Record that a volume is attached to a specific node."""
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                resource_loader.load_text("sql", "controller", "upsert_volume_attachments.sql"),
                (app_name, volume_name, node_id, retention, now),
            )
            conn.commit()

    def list_volume_attachments(self, app_name: str) -> list[VolumeAttachment]:
        """Return recorded volume attachments for an app."""
        with self._connect() as conn:
            rows = conn.execute(
                resource_loader.load_text(
                    "sql", "controller", "select_volume_attachments_by_app.sql"
                ),
                (app_name,),
            ).fetchall()
        out: list[VolumeAttachment] = []
        for row in rows:
            try:
                created = datetime.fromisoformat(row[4])
            except Exception:
                created = datetime.fromtimestamp(0, tz=timezone.utc)
            out.append(
                VolumeAttachment(
                    app_name=row[0],
                    volume_name=row[1],
                    node_id=row[2],
                    retention=row[3],
                    created_at=created,
                )
            )
        return out

    def delete_volume_attachments(self, app_name: str) -> None:
        """Remove all volume attachments for an app (e.g., on delete)."""
        with self._connect() as conn:
            conn.execute("DELETE FROM volume_attachments WHERE app_name = ?", (app_name,))
            conn.commit()

    # --- Admin / maintenance helpers ---
    def delete_app_state(self, app_name: str, *, purge_history: bool = False) -> None:
        """Remove status and pod rows for an app. Optionally purge events and revisions.

        Does not affect running containers; the runtime is responsible for removing them.
        """
        with self._connect() as conn:
            conn.execute("DELETE FROM pod_status WHERE app_name = ?", (app_name,))
            conn.execute("DELETE FROM pod_nodes WHERE app_name = ?", (app_name,))
            conn.execute("DELETE FROM app_status WHERE app_name = ?", (app_name,))
            conn.execute("DELETE FROM volume_attachments WHERE app_name = ?", (app_name,))
            if purge_history:
                conn.execute("DELETE FROM app_events WHERE app_name = ?", (app_name,))
                conn.execute("DELETE FROM app_revisions WHERE app_name = ?", (app_name,))
            conn.commit()


def _outbox_publish_subject(site_id: str) -> str:
    return f"k1s.v1.work.site.{site_id}"


def _outbox_publish_msg_id(work_id: str, attempt: int, payload: dict) -> str:
    return str(payload.get("operation_id") or f"{work_id}:{attempt}")


def state_store_from_env() -> SQLiteStateStore:
    backend = (os.getenv("AE_STATE_BACKEND") or "").strip().lower()
    if backend == "etcd":
        from ae.controller.etcd_state import EtcdStateStore

        return EtcdStateStore()
    dsn = os.getenv("AE_STATE_DSN")
    db_path = Path(os.getenv("AE_STATE_DB", "state/controller.db"))
    if not dsn:
        db_path.parent.mkdir(parents=True, exist_ok=True)
    return SQLiteStateStore(db_path, dsn=dsn)


class _PgCompatConnection:
    """Light wrapper to allow sqlite-style '?' placeholders on psycopg connections."""

    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql: str, params=()):
        sql = sql.replace("?", "%s")
        cur = self._conn.cursor()
        cur.execute(sql, params)
        return cur

    def executemany(self, sql: str, seq):
        sql = sql.replace("?", "%s")
        cur = self._conn.cursor()
        cur.executemany(sql, seq)
        return cur

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self._conn.commit()
            else:
                self._conn.rollback()
        finally:
            self._conn.close()
        return False


# ruff: noqa
