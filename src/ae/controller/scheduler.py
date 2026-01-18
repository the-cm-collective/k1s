# ruff: noqa: E501,S112,SIM110
"""Replica placement planner for multi-node scheduling (Phase 4).

The scheduler is intentionally lightweight:
- Filters nodes by readiness, staleness, cordon, nodeSelector, and taints/tolerations.
- Defaults to round-robin across eligible nodes.
- Pins all replicas to a single node when persistent storage is declared to avoid
  cross-node volume assumptions.
- Falls back to the local runtime when no nodes are eligible.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone

from ae.controller.spec import AppManifest, app_key_for_manifest
from ae.controller.state import NodeRecord, NodeStatus, SQLiteStateStore


@dataclass(slots=True)
class Placement:
    """Replica placement target."""

    node: NodeRecord | None
    agent_url: str | None
    replica_ids: list[str]


class Scheduler:
    """Minimal scheduler that distributes replicas across Ready nodes."""

    def __init__(self, store: SQLiteStateStore) -> None:
        self._store = store

    def plan(self, manifest: AppManifest, revision: int) -> tuple[list[Placement], list[str]]:
        """Return placements and warnings for the manifest."""
        desired = int(manifest.spec.replicas)
        app_name = app_key_for_manifest(manifest)
        replica_ids = [f"{app_name}-rev{revision}-{i}" for i in range(desired)]
        warnings: list[str] = []

        nodes = self._store.list_nodes()
        grace = self._not_ready_grace_seconds()
        now = datetime.now(timezone.utc)

        eligible: list[NodeRecord] = []
        stale_nodes: list[str] = []
        for node, status in nodes:
            if bool(getattr(node, "cordoned", False)):
                continue
            if not self._is_ready(status, now, grace):
                stale_nodes.append(node.node_id)
                continue
            if not self._matches_node_selector(node, manifest):
                continue
            if not self._tolerates_taints(node, manifest):
                continue
            eligible.append(node)

        if not eligible:
            warnings.append("no eligible nodes; falling back to local runtime")
            return [Placement(node=None, agent_url=None, replica_ids=replica_ids)], warnings

        # Storage pinning: keep all replicas on one node when storage is declared.
        has_storage = bool(getattr(manifest.spec, "storage", None))
        if has_storage:
            bound_node_id = None
            try:
                bindings = self._store.list_storage_bindings(app_name)
                if bindings:
                    bound_node_id = bindings[0].node_id
            except Exception:
                bound_node_id = None

            if bound_node_id:
                bound_node = next((n for n in eligible if n.node_id == bound_node_id), None)
                if bound_node is not None:
                    return [
                        Placement(
                            node=bound_node, agent_url=bound_node.endpoint, replica_ids=replica_ids
                        )
                    ], warnings
                # Bound node exists but is not eligible (cordoned/stale/missing)
                warnings.append(
                    f"storage volumes bound to node {bound_node_id} but node is not eligible (cordoned/not ready); skipping placement"
                )
                if stale_nodes:
                    warnings.append(f"stale/not-ready nodes skipped: {', '.join(stale_nodes)}")
                return [], warnings

            target = eligible[0]
            return [
                Placement(node=target, agent_url=target.endpoint, replica_ids=replica_ids)
            ], warnings

        # Topology spread / soft anti-affinity: if topologySpreadConstraints specify a
        # topologyKey and we have >1 eligible node, distribute replicas to minimize skew
        # across that key. This is a lightweight best-effort implementation.
        spread_key = self._topology_key(manifest)
        if spread_key and len(eligible) > 1:
            placements = self._spread_by_topology(eligible, replica_ids, spread_key)
            if placements:
                if stale_nodes:
                    warnings.append(f"stale/not-ready nodes skipped: {', '.join(stale_nodes)}")
                return placements, warnings

        # Round-robin placement across eligible nodes.
        assignments: dict[str, list[str]] = {n.node_id: [] for n in eligible}
        for idx, rid in enumerate(replica_ids):
            node = eligible[idx % len(eligible)]
            assignments[node.node_id].append(rid)

        placements: list[Placement] = []
        for node in eligible:
            ids = assignments.get(node.node_id, [])
            if ids:
                placements.append(Placement(node=node, agent_url=node.endpoint, replica_ids=ids))

        if stale_nodes:
            warnings.append(f"stale/not-ready nodes skipped: {', '.join(stale_nodes)}")

        return placements or [
            Placement(node=None, agent_url=None, replica_ids=replica_ids)
        ], warnings

    def _not_ready_grace_seconds(self) -> int:
        try:
            return int(os.getenv("AE_NODE_NOTREADY_AFTER", "40") or 40)
        except Exception:
            return 40

    @staticmethod
    def _is_ready(status: NodeStatus | None, now: datetime, grace: int) -> bool:
        if status is None:
            return False
        try:
            age = (now - status.seen_at).total_seconds()
        except Exception:
            age = grace + 1
        if age > grace:
            return False
        return str(status.status or "").lower() == "ready"

    @staticmethod
    def _matches_node_selector(node: NodeRecord, manifest: AppManifest) -> bool:
        selector = getattr(manifest.spec, "node_selector", {}) or {}
        if not selector:
            return True
        labels = node.labels or {}
        for key, val in selector.items():
            if labels.get(key) != val:
                return False
        return True

    @staticmethod
    def _tolerates_taints(node: NodeRecord, manifest: AppManifest) -> bool:
        taints = node.taints or []
        tolerations = getattr(manifest.spec, "tolerations", []) or []
        if not taints:
            return True
        for t in taints:
            try:
                key = t.get("key")
                effect = t.get("effect")
                value = t.get("value")
            except Exception:
                continue
            matched = False
            for tol in tolerations:
                if tol.get("key") != key:
                    continue
                if tol.get("effect") and tol.get("effect") != effect:
                    continue
                if tol.get("value") not in (None, "", value):
                    continue
                matched = True
                break
            if not matched and effect in {"NoSchedule", "NoExecute"}:
                return False
        return True

    @staticmethod
    def _topology_key(manifest: AppManifest) -> str | None:
        constraints = getattr(manifest.spec, "topology_spread_constraints", []) or []
        for c in constraints:
            key = c.get("topologyKey") if isinstance(c, dict) else None
            if key:
                return key
        return None

    @staticmethod
    def _spread_by_topology(
        nodes: list[NodeRecord], replica_ids: list[str], topology_key: str
    ) -> list[Placement]:
        nodes_by_val: dict[str, list[NodeRecord]] = {}
        for n in nodes:
            val = (n.labels or {}).get(topology_key)
            if val is None:
                continue
            nodes_by_val.setdefault(str(val), []).append(n)
        if len(nodes_by_val) <= 1:
            return []

        counts: dict[str, int] = {k: 0 for k in nodes_by_val}
        node_index: dict[str, int] = {k: 0 for k in nodes_by_val}
        assignments: dict[str, list[str]] = {n.node_id: [] for n in nodes}

        for rid in replica_ids:
            # choose topology value with the smallest current count (ties by key)
            val = sorted(counts.items(), key=lambda kv: (kv[1], kv[0]))[0][0]
            bucket = nodes_by_val[val]
            idx = node_index[val] % len(bucket)
            node_index[val] += 1
            counts[val] += 1
            target = bucket[idx]
            assignments[target.node_id].append(rid)

        placements: list[Placement] = []
        for n in nodes:
            ids = assignments.get(n.node_id, [])
            if ids:
                placements.append(Placement(node=n, agent_url=n.endpoint, replica_ids=ids))
        return placements


# ruff: noqa
