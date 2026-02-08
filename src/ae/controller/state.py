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

from ae.controller.health import HealthReport
from ae.controller.spec import AppManifest, app_key_for_manifest
from ae.resources import loader as resource_loader
from ae.runtime import RuntimeResult


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


@dataclass(slots=True)
class RegistryEntry:
    """Registered desired-state manifest for reconciliation."""

    app_name: str
    manifest: AppManifest
    spec_hash: str
    source: str
    labels: dict
    updated_at: datetime


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
    publish_attempts: int


@dataclass(slots=True)
class WorkLedgerEntry:
    work_id: str
    attempt: int
    site_id: str
    state: str
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
class StorageBinding:
    """Mapping of a persistent volume to the node that owns it."""

    app_name: str
    volume_name: str
    node_id: str
    retention: str | None
    created_at: datetime


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
    taints: list
    backend: str | None
    endpoint: str | None
    pod_cidr: str | None
    wg_pubkey: str | None
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
                "storage_bindings",
                [
                    "app_name",
                    "volume_name",
                    "node_id",
                    "retention",
                    "created_at",
                ],
            ):
                conn.execute("DROP TABLE IF EXISTS storage_bindings")
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
            conn.execute(
                resource_loader.load_text("sql", "controller", "create_app_status.sql")
            )
            conn.execute(
                resource_loader.load_text("sql", "controller", "create_app_registry.sql")
            )
            conn.execute(
                resource_loader.load_text("sql", "controller", "create_pod_status.sql")
            )
            conn.execute(
                resource_loader.render_text(
                    "sql",
                    "controller",
                    "create_probe_history.sql",
                    AUTO_INC=auto_inc,
                )
            )
            conn.execute(
                resource_loader.load_text("sql", "controller", "create_pod_nodes.sql")
            )
            conn.execute(
                resource_loader.load_text("sql", "controller", "create_app_revisions.sql")
            )
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
            conn.execute(
                resource_loader.load_text("sql", "controller", "create_services.sql")
            )
            conn.execute(
                resource_loader.load_text(
                    "sql", "controller", "create_service_endpoints.sql"
                )
            )
            conn.execute(
                resource_loader.load_text("sql", "controller", "create_nodes.sql")
            )
            conn.execute(
                resource_loader.load_text(
                    "sql", "controller", "create_node_heartbeats.sql"
                )
            )
            self._execute_script(
                conn,
                resource_loader.load_text("sql", "controller", "create_node_leases.sql"),
            )
            conn.execute(
                resource_loader.load_text(
                    "sql", "controller", "create_storage_bindings.sql"
                )
            )
            conn.execute(
                resource_loader.load_text(
                    "sql", "controller", "create_volume_attachments.sql"
                )
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
                resource_loader.load_text(
                    "sql", "controller", "create_site_ingress_endpoints.sql"
                ),
            )
            self._execute_script(
                conn,
                resource_loader.load_text(
                    "sql", "controller", "create_edge_ingress_routes.sql"
                ),
            )
            self._execute_script(
                conn,
                resource_loader.load_text(
                    "sql", "controller", "create_edge_ingress_policies.sql"
                ),
            )
            self._ensure_column(conn, "edge_ingress_routes", "status_json", "TEXT")
            self._ensure_column(conn, "edge_ingress_policies", "status_json", "TEXT")
            self._ensure_column(conn, "pod_status", "endpoint", "TEXT")
            self._migrate_storage_bindings(conn)
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
            conn.execute(
                f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {col_type}"
            )
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

    def _migrate_storage_bindings(self, conn) -> None:
        """Best-effort migration from legacy storage_bindings to volume_attachments."""
        try:
            rows = conn.execute(
                resource_loader.load_text(
                    "sql", "controller", "select_storage_bindings_all.sql"
                )
            ).fetchall()
        except Exception:
            return
        if not rows:
            return
        try:
            existing = conn.execute(
                resource_loader.load_text("sql", "controller", "count_volume_attachments.sql")
            ).fetchone()
            if existing and int(existing[0]) > 0:
                return
        except Exception:
            return
        for row in rows:
            try:
                conn.execute(
                    resource_loader.load_text(
                        "sql", "controller", "insert_volume_attachments_ignore.sql"
                    ),
                    (row[0], row[1], row[2], row[3], row[4]),
                )
            except Exception:
                continue

    def record_snapshot(
        self,
        manifest: AppManifest,
        runtime_result: RuntimeResult,
        health_report: HealthReport,
        revision: int,
        revision_status: str,
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
                    resource_loader.load_text(
                        "sql", "controller", "insert_pod_status.sql"
                    ),
                    rows,
                )

            try:
                ttl_seconds = int(
                    os.getenv("AE_POD_STATUS_TTL_SECONDS", "30") or "30"
                )
            except Exception:
                ttl_seconds = 30
            if ttl_seconds > 0:
                cutoff = datetime.now(timezone.utc) - timedelta(seconds=ttl_seconds)
                conn.execute(
                    "DELETE FROM pod_status WHERE app_name = ? AND (updated_at IS NULL OR updated_at < ?)",
                    (app_name, cutoff.isoformat()),
                )
            try:
                node_ttl_seconds = int(
                    os.getenv("AE_POD_NODE_TTL_SECONDS", "300") or "300"
                )
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
                    resource_loader.load_text(
                        "sql", "controller", "insert_probe_history.sql"
                    ),
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
                    resource_loader.load_text(
                        "sql", "controller", "insert_pod_nodes_upsert.sql"
                    ),
                    node_rows,
                )
            conn.execute(
                resource_loader.load_text(
                    "sql", "controller", "update_app_revisions_status.sql"
                ),
                (revision_status, manifest.spec.image, app_name, revision),
            )
            conn.commit()

    def get_status(self, app_name: str) -> AppStatus | None:
        with self._connect() as conn:
            row = conn.execute(
                resource_loader.load_text(
                    "sql", "controller", "select_app_status_by_name.sql"
                ),
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
                ingress_host=row[10],
                ingress_path=row[11],
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
                ingress_host=row[10],
                ingress_path=row[11],
            )
            for row in rows
        ]

    def list_pods(self, app_name: str) -> list[PodStatus]:
        with self._connect() as conn:
            rows = conn.execute(
                resource_loader.load_text(
                    "sql", "controller", "select_pod_status_by_app.sql"
                ),
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

    def list_replicas(self, app_name: str) -> list[PodStatus]:
        """Alias for list_pods (deprecated)."""
        return self.list_pods(app_name)

    def list_pod_nodes(self, app_name: str) -> list[tuple[str, str]]:
        with self._connect() as conn:
            rows = conn.execute(
                resource_loader.load_text(
                    "sql", "controller", "select_pod_nodes_with_status.sql"
                ),
                (app_name, app_name),
            ).fetchall()
        return [(row[0], row[1], row[2], row[3], row[4], row[5], row[6]) for row in rows]

    def list_replica_nodes(self, app_name: str) -> list[tuple[str, str]]:
        """Alias for list_pod_nodes (deprecated)."""
        return self.list_pod_nodes(app_name)

    def set_pod_nodes(self, app_name: str, placements: list[tuple[str, str]]) -> None:
        """Replace placement mapping for an app."""
        ts = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute("DELETE FROM pod_nodes WHERE app_name = ?", (app_name,))
            if placements:
                conn.executemany(
                    resource_loader.load_text(
                        "sql", "controller", "insert_pod_nodes.sql"
                    ),
                    [(app_name, rid, nid, ts) for rid, nid in placements],
                )
            conn.commit()

    def set_replica_nodes(self, app_name: str, placements: list[tuple[str, str]]) -> None:
        """Alias for set_pod_nodes (deprecated)."""
        self.set_pod_nodes(app_name, placements)

    def get_probe_history(self, app_name: str, limit: int) -> list[ProbeHistoryEntry]:
        with self._connect() as conn:
            rows = conn.execute(
                resource_loader.load_text(
                    "sql", "controller", "select_probe_history_by_app.sql"
                ),
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
    ) -> None:
        spec_json = json.dumps(manifest.model_dump(by_alias=True), sort_keys=True)
        spec_hash = self._manifest_hash(manifest)
        updated_at = datetime.now(timezone.utc).isoformat()
        app_name = app_key_for_manifest(manifest)
        existing_source = None
        existing_labels: dict | None = None
        if source is None or labels is None:
            try:
                with self._connect() as conn:
                    row = conn.execute(
                        "SELECT source, labels FROM app_registry WHERE app_name = ?",
                        (app_name,),
                    ).fetchone()
                if row is not None:
                    existing_source = row[0]
                    try:
                        existing_labels = json.loads(row[1] or "{}")
                        if not isinstance(existing_labels, dict):
                            existing_labels = {}
                    except Exception:
                        existing_labels = {}
            except Exception:
                existing_source = None
                existing_labels = None
        labels_json = json.dumps(
            labels if labels is not None else (existing_labels or {}),
            sort_keys=True,
        )
        source_val = str(source or existing_source or "unknown")
        with self._connect() as conn:
            conn.execute(
                resource_loader.load_text(
                    "sql", "controller", "insert_app_registry_upsert.sql"
                ),
                (
                    app_name,
                    spec_hash,
                    spec_json,
                    source_val,
                    labels_json,
                    updated_at,
                ),
            )
            conn.commit()

    def list_registered_apps(self) -> list[RegistryEntry]:
        with self._connect() as conn:
            rows = conn.execute(
                resource_loader.load_text(
                    "sql", "controller", "select_app_registry_all.sql"
                )
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
                resource_loader.load_text(
                    "sql", "controller", "select_app_registry_by_name.sql"
                ),
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

    def delete_registered_app(self, app_name: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM app_registry WHERE app_name = ?", (app_name,))
            conn.commit()

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
                resource_loader.load_text(
                    "sql", "controller", "select_revision_spec_json.sql"
                ),
                (app_name, revision),
            ).fetchone()
        if row is None:
            raise ValueError(f"No revision {revision} recorded for {app_name}")
        return AppManifest.model_validate_json(row[0])

    def list_revisions(self, app_name: str, limit: int = 10) -> list[RevisionInfo]:
        with self._connect() as conn:
            rows = conn.execute(
                resource_loader.load_text(
                    "sql", "controller", "select_revisions_by_app.sql"
                ),
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
                resource_loader.load_text(
                    "sql", "controller", "select_app_events_by_app.sql"
                ),
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
                resource_loader.load_text(
                    "sql", "controller", "select_app_events_paginated.sql"
                ),
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
                    self.update_work_state(
                        work_id=work_id, attempt=attempt, state="Dispatched"
                    )
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
                resource_loader.load_text(
                    "sql", "controller", "select_edge_ingress_routes_all.sql"
                )
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
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM work_outbox WHERE work_id = ? AND attempt = ?",
                (work_id, attempt),
            )
            conn.execute(
                """
                INSERT INTO work_outbox
                  (work_id, attempt, site_id, payload_json, state, publish_attempts,
                   last_publish_at, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    work_id,
                    int(attempt),
                    site_id,
                    payload_json,
                    "Unpublished",
                    0,
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
                SELECT work_id, attempt, site_id, payload_json, publish_attempts
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
                    publish_attempts=int(row[4] or 0),
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
                    last_publish_at = ?, updated_at = ?
                WHERE work_id = ? AND attempt = ?
                """,
                ("Published", now, now, work_id, int(attempt)),
            )
            conn.commit()

    def record_outbox_publish_attempt(self, work_id: str, attempt: int) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE work_outbox
                SET publish_attempts = publish_attempts + 1, last_publish_at = ?, updated_at = ?
                WHERE work_id = ? AND attempt = ?
                """,
                (now, now, work_id, int(attempt)),
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
                    SET attempt = ?, site_id = ?, state = ?, desired_generation = ?,
                        updated_at = ?, state_updated_at = ?
                    WHERE work_id = ?
                    """,
                    (
                        int(attempt),
                        site_id,
                        state,
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
                      (work_id, attempt, site_id, state, desired_generation,
                       assigned_node_id, observed_generation, result_json,
                       created_at, updated_at, state_updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        work_id,
                        int(attempt),
                        site_id,
                        state,
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
                SELECT work_id, attempt, site_id, state, desired_generation,
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
            if row[7]:
                try:
                    result = json.loads(row[7])
                except Exception:
                    result = None
            return WorkLedgerEntry(
                work_id=str(row[0]),
                attempt=int(row[1]),
                site_id=str(row[2]),
                state=str(row[3]),
                desired_generation=int(row[4]) if row[4] is not None else None,
                assigned_node_id=str(row[5]) if row[5] else None,
                observed_generation=int(row[6]) if row[6] is not None else None,
                result=result,
                created_at=datetime.fromisoformat(row[8]),
                updated_at=datetime.fromisoformat(row[9]),
                state_updated_at=datetime.fromisoformat(row[10]),
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

    def list_work_state_before(
        self, state: str, cutoff: datetime
    ) -> list[WorkLedgerEntry]:
        cutoff_iso = cutoff.isoformat()
        rows: list[WorkLedgerEntry] = []
        with self._connect() as conn:
            results = conn.execute(
                """
                SELECT work_id, attempt, site_id, state, desired_generation,
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
                if row[7]:
                    try:
                        result = json.loads(row[7])
                    except Exception:
                        result = None
                rows.append(
                    WorkLedgerEntry(
                        work_id=str(row[0]),
                        attempt=int(row[1]),
                        site_id=str(row[2]),
                        state=str(row[3]),
                        desired_generation=int(row[4]) if row[4] is not None else None,
                        assigned_node_id=str(row[5]) if row[5] else None,
                        observed_generation=int(row[6]) if row[6] is not None else None,
                        result=result,
                        created_at=datetime.fromisoformat(row[8]),
                        updated_at=datetime.fromisoformat(row[9]),
                        state_updated_at=datetime.fromisoformat(row[10]),
                    )
                )
        return rows

    def reschedule_work(
        self,
        *,
        work_id: str,
        attempt: int,
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
            cursor = conn.execute(
                """
                UPDATE work_ledger
                SET attempt = ?, state = ?, updated_at = ?, state_updated_at = ?
                WHERE work_id = ? AND attempt = ?
                """,
                (
                    new_attempt,
                    "Pending",
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
                  (work_id, attempt, site_id, payload_json, state, publish_attempts,
                   last_publish_at, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    work_id,
                    new_attempt,
                    site_id,
                    json.dumps(payload),
                    "Unpublished",
                    0,
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
                resource_loader.load_text(
                    "sql", "controller", "select_rollout_canary.sql"
                ),
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
                (ep.app_name, ep.port, ep.ip, ep.target_port, int(ep.ready))
                for ep in endpoints
            ]
            if rows:
                conn.executemany(
                    resource_loader.load_text(
                        "sql", "controller", "insert_service_endpoints.sql"
                    ),
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
                    resource_loader.load_text(
                        "sql", "controller", "insert_service_endpoints.sql"
                    ),
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
        taints: list | None = None,
        backend: str | None = None,
        endpoint: str | None = None,
        pod_cidr: str | None = None,
        wg_pubkey: str | None = None,
        cordoned: bool | None = None,
    ) -> None:
        if cordoned is None:
            cordoned = self._get_node_cordoned(node_id)
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                resource_loader.load_text("sql", "controller", "upsert_nodes.sql"),
                (
                    node_id,
                    name,
                    json.dumps(labels or {}, sort_keys=True),
                    json.dumps(taints or [], sort_keys=True),
                    backend,
                    endpoint,
                    pod_cidr,
                    wg_pubkey,
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
                resource_loader.load_text(
                    "sql", "controller", "select_nodes_with_heartbeat.sql"
                )
            ).fetchall()
        result: list[tuple[NodeRecord, NodeStatus | None]] = []
        for row in rows:
            labels = {}
            taints = []
            try:
                labels = json.loads(row[2] or "{}")
            except Exception:
                labels = {}
            try:
                taints = json.loads(row[3] or "[]")
            except Exception:
                taints = []
            try:
                created = datetime.fromisoformat(row[8])
            except Exception:
                created = datetime.fromtimestamp(0, tz=timezone.utc)
            try:
                updated = datetime.fromisoformat(row[9])
            except Exception:
                updated = datetime.fromtimestamp(0, tz=timezone.utc)
            node = NodeRecord(
                node_id=row[0],
                name=row[1],
                labels=labels,
                taints=taints,
                backend=row[4],
                endpoint=row[5],
                pod_cidr=row[6],
                wg_pubkey=row[7],
                cordoned=bool(row[10]),
                created_at=created,
                updated_at=updated,
            )
            status = None
            if row[11] is not None:
                try:
                    seen_at = datetime.fromisoformat(row[12])
                except Exception:
                    seen_at = datetime.fromtimestamp(0, tz=timezone.utc)
                status = NodeStatus(node_id=row[0], status=row[11], seen_at=seen_at)
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
        taints = []
        try:
            labels = json.loads(row[2] or "{}")
        except Exception:
            labels = {}
        try:
            taints = json.loads(row[3] or "[]")
        except Exception:
            taints = []
        try:
            created = datetime.fromisoformat(row[8])
        except Exception:
            created = datetime.fromtimestamp(0, tz=timezone.utc)
        try:
            updated = datetime.fromisoformat(row[9])
        except Exception:
            updated = datetime.fromtimestamp(0, tz=timezone.utc)
        node = NodeRecord(
            node_id=row[0],
            name=row[1],
            labels=labels,
            taints=taints,
            backend=row[4],
            endpoint=row[5],
            pod_cidr=row[6],
            wg_pubkey=row[7],
            cordoned=bool(row[10]),
            created_at=created,
            updated_at=updated,
        )
        status = None
        if row[11] is not None:
            try:
                seen_at = datetime.fromisoformat(row[12])
            except Exception:
                seen_at = datetime.fromtimestamp(0, tz=timezone.utc)
            status = NodeStatus(node_id=row[0], status=row[11], seen_at=seen_at)
        return node, status

    # --- Volume attachments --------------------------------------------

    def upsert_volume_attachment(
        self, app_name: str, volume_name: str, node_id: str, retention: str | None = None
    ) -> None:
        """Record that a volume is attached to a specific node."""
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                resource_loader.load_text(
                    "sql", "controller", "upsert_volume_attachments.sql"
                ),
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
            # Back-compat: if no attachments are present, consult storage_bindings.
            if not rows:
                rows = conn.execute(
                    resource_loader.load_text(
                        "sql", "controller", "select_storage_bindings_by_app.sql"
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

    # --- Storage bindings (legacy) ------------------------------------

    def upsert_storage_binding(
        self, app_name: str, volume_name: str, node_id: str, retention: str | None = None
    ) -> None:
        """Record that a persistent volume resides on a specific node."""
        self.upsert_volume_attachment(app_name, volume_name, node_id, retention)

    def list_storage_bindings(self, app_name: str) -> list[StorageBinding]:
        """Return recorded bindings for an app's persistent volumes."""
        out: list[StorageBinding] = []
        for att in self.list_volume_attachments(app_name):
            out.append(
                StorageBinding(
                    app_name=att.app_name,
                    volume_name=att.volume_name,
                    node_id=att.node_id,
                    retention=att.retention,
                    created_at=att.created_at,
                )
            )
        return out

    def delete_storage_bindings(self, app_name: str) -> None:
        """Remove all bindings for an app (e.g., on delete)."""
        self.delete_volume_attachments(app_name)

    # --- Admin / maintenance helpers ---
    def delete_app_state(self, app_name: str, *, purge_history: bool = False) -> None:
        """Remove status and pod rows for an app. Optionally purge events and revisions.

        Does not affect running containers; the runtime is responsible for removing them.
        """
        with self._connect() as conn:
            conn.execute("DELETE FROM pod_status WHERE app_name = ?", (app_name,))
            conn.execute("DELETE FROM pod_nodes WHERE app_name = ?", (app_name,))
            conn.execute("DELETE FROM app_status WHERE app_name = ?", (app_name,))
            conn.execute("DELETE FROM storage_bindings WHERE app_name = ?", (app_name,))
            conn.execute("DELETE FROM volume_attachments WHERE app_name = ?", (app_name,))
            if purge_history:
                conn.execute("DELETE FROM app_events WHERE app_name = ?", (app_name,))
                conn.execute("DELETE FROM app_revisions WHERE app_name = ?", (app_name,))
            conn.commit()


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
