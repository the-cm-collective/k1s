"""Shared HA mutation fencing helpers."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass(slots=True, frozen=True)
class ControllerMutationIdentity:
    controller_id: str
    controller_epoch: int

    def envelope(self, operation_id: str) -> "MutationEnvelope":
        return MutationEnvelope(
            controller_id=self.controller_id,
            controller_epoch=int(self.controller_epoch),
            operation_id=operation_id,
        )


@dataclass(slots=True, frozen=True)
class MutationEnvelope:
    controller_id: str
    controller_epoch: int
    operation_id: str

    def as_dict(self) -> dict[str, object]:
        return {
            "controller_id": self.controller_id,
            "controller_epoch": int(self.controller_epoch),
            "operation_id": self.operation_id,
        }


@dataclass(slots=True, frozen=True)
class FenceScopeState:
    scope: str
    controller_id: str
    controller_epoch: int
    updated_at: datetime


@dataclass(slots=True, frozen=True)
class FenceDecision:
    status: str
    current: FenceScopeState | None
    epoch_advanced: bool = False

    @property
    def accepted(self) -> bool:
        return self.status == "accept"

    @property
    def duplicate(self) -> bool:
        return self.status == "duplicate"

    @property
    def stale(self) -> bool:
        return self.status == "stale"


class SQLiteFenceStore:
    """Persist accepted controller epochs and operation IDs per executor scope."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)

    def init(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS fence_scopes (
                  scope TEXT PRIMARY KEY,
                  controller_id TEXT NOT NULL,
                  controller_epoch INTEGER NOT NULL,
                  updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS fence_operations (
                  scope TEXT NOT NULL,
                  operation_id TEXT NOT NULL,
                  controller_id TEXT NOT NULL,
                  controller_epoch INTEGER NOT NULL,
                  committed_at TEXT NOT NULL,
                  PRIMARY KEY (scope, operation_id)
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS fence_operations_scope_epoch_idx
                ON fence_operations(scope, controller_epoch)
                """
            )
            conn.commit()

    def current(self, scope: str) -> FenceScopeState | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT controller_id, controller_epoch, updated_at
                FROM fence_scopes
                WHERE scope = ?
                """,
                (scope,),
            ).fetchone()
        return _scope_state(scope, row)

    def begin(self, scope: str, envelope: MutationEnvelope) -> FenceDecision:
        now = _now_iso()
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT controller_id, controller_epoch, updated_at
                FROM fence_scopes
                WHERE scope = ?
                """,
                (scope,),
            ).fetchone()
            current = _scope_state(scope, row)
            if current is not None:
                if int(envelope.controller_epoch) < int(current.controller_epoch):
                    return FenceDecision(status="stale", current=current)
                if (
                    int(envelope.controller_epoch) == int(current.controller_epoch)
                    and envelope.controller_id != current.controller_id
                ):
                    return FenceDecision(status="stale", current=current)
            op_row = conn.execute(
                """
                SELECT controller_id, controller_epoch, committed_at
                FROM fence_operations
                WHERE scope = ? AND operation_id = ?
                """,
                (scope, envelope.operation_id),
            ).fetchone()
            if op_row is not None:
                return FenceDecision(status="duplicate", current=current)
            epoch_advanced = current is None or int(envelope.controller_epoch) > int(
                current.controller_epoch
            )
            if current is None:
                conn.execute(
                    """
                    INSERT INTO fence_scopes (scope, controller_id, controller_epoch, updated_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (scope, envelope.controller_id, int(envelope.controller_epoch), now),
                )
            elif epoch_advanced:
                conn.execute(
                    """
                    UPDATE fence_scopes
                    SET controller_id = ?, controller_epoch = ?, updated_at = ?
                    WHERE scope = ?
                    """,
                    (envelope.controller_id, int(envelope.controller_epoch), now, scope),
                )
            else:
                conn.execute(
                    "UPDATE fence_scopes SET updated_at = ? WHERE scope = ?",
                    (now, scope),
                )
            conn.commit()
        return FenceDecision(status="accept", current=self.current(scope), epoch_advanced=epoch_advanced)

    def commit(self, scope: str, envelope: MutationEnvelope) -> None:
        now = _now_iso()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO fence_operations
                  (scope, operation_id, controller_id, controller_epoch, committed_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    scope,
                    envelope.operation_id,
                    envelope.controller_id,
                    int(envelope.controller_epoch),
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO fence_scopes (scope, controller_id, controller_epoch, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(scope) DO UPDATE SET
                  controller_id = excluded.controller_id,
                  controller_epoch = excluded.controller_epoch,
                  updated_at = excluded.updated_at
                """,
                (scope, envelope.controller_id, int(envelope.controller_epoch), now),
            )
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn


def parse_envelope(payload: dict | None) -> MutationEnvelope | None:
    if not isinstance(payload, dict):
        return None
    controller_id = str(payload.get("controller_id") or "").strip()
    operation_id = str(payload.get("operation_id") or "").strip()
    try:
        controller_epoch = int(payload.get("controller_epoch") or 0)
    except Exception:
        controller_epoch = 0
    if not controller_id or not operation_id or controller_epoch <= 0:
        return None
    return MutationEnvelope(
        controller_id=controller_id,
        controller_epoch=controller_epoch,
        operation_id=operation_id,
    )


def lease_operation(request_id: str) -> str:
    return str(request_id).strip()


def work_operation(work_id: str, attempt: int) -> str:
    return f"work:{work_id}:{int(attempt)}"


def route_operation(site_id: str, bundle_rev: int, controller_epoch: int) -> str:
    return f"route:{site_id}:{int(bundle_rev)}:{int(controller_epoch)}"


def ensure_operation(app_name: str, revision: int, node_id: str) -> str:
    return f"ensure:{app_name}:{int(revision)}:{node_id}"


def gc_operation(app_name: str, keep_revision: int, node_id: str) -> str:
    return f"gc:{app_name}:{int(keep_revision)}:{node_id}"


def delete_operation(app_name: str, node_id: str, controller_epoch: int) -> str:
    return f"delete:{app_name}:{int(controller_epoch)}:{node_id}"


def fabric_ensure_operation(session_id: str, node_id: str) -> str:
    return f"fabric.ensure:{session_id}:{node_id}"


def fabric_teardown_operation(session_id: str, node_id: str) -> str:
    return f"fabric.teardown:{session_id}:{node_id}"


def _scope_state(scope: str, row) -> FenceScopeState | None:
    if row is None:
        return None
    return FenceScopeState(
        scope=scope,
        controller_id=str(row[0]),
        controller_epoch=int(row[1]),
        updated_at=datetime.fromisoformat(str(row[2])),
    )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
