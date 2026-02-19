# ruff: noqa: E501,S112,SIM110
"""Pod placement planner for multi-node scheduling.

The scheduler is intentionally lightweight:
- Filters nodes by readiness, staleness, cordon, nodeSelector, and taints/tolerations.
- Defaults to round-robin across eligible nodes.
- Pins all pods to a single node when persistent storage is declared to avoid
  cross-node volume assumptions.
- Falls back to the local runtime when no nodes are registered.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ae.controller.spec import AppManifest, app_key_for_manifest
from ae.controller.state import NodeRecord, NodeStatus, SQLiteStateStore
from ae.storage.config import DEFAULT_CLASS_ANNOTATIONS

SC_GROUP = "storage.k8s.io"
SC_VERSION = "v1"
SC_RESOURCE = "storageclasses"
CORE_GROUP = ""
CORE_VERSION = "v1"
PVC_RESOURCE = "persistentvolumeclaims"
PV_RESOURCE = "persistentvolumes"
LOCAL_PATH_PROVISIONER = "k1s.io/local-path"
WAIT_FOR_FIRST_CONSUMER = "WaitForFirstConsumer"
SELECTED_NODE_ANNOTATION = "volume.kubernetes.io/selected-node"


@dataclass(slots=True)
class Placement:
    """Pod placement target."""

    node: NodeRecord | None
    agent_url: str | None
    pod_names: list[str]

    @property
    def replica_ids(self) -> list[str]:
        return self.pod_names

    @replica_ids.setter
    def replica_ids(self, value: list[str]) -> None:
        self.pod_names = value


class Scheduler:
    """Minimal scheduler that distributes pods across Ready nodes."""

    def __init__(self, store: SQLiteStateStore) -> None:
        self._store = store
        self._shim_store = self._init_shim_store()
        self._default_sc_name: str | None = None

    def plan(self, manifest: AppManifest, revision: int) -> tuple[list[Placement], list[str]]:
        """Return placements and warnings for the manifest."""
        desired = int(manifest.spec.replicas)
        app_name = app_key_for_manifest(manifest)
        warnings: list[str] = []

        rwop_claims = self._rwop_claims(manifest)
        if rwop_claims and desired > 1:
            warnings.append(
                "ReadWriteOncePod PVCs limit replicas to 1; "
                f"claims: {', '.join(rwop_claims)}"
            )
            desired = 1

        pod_names = [f"{app_name}-rev{revision}-{i}" for i in range(desired)]
        nodes = self._store.list_nodes()
        if not nodes:
            warnings.append("no nodes registered; scheduling on local runtime")
            return [Placement(node=None, agent_url=None, pod_names=pod_names)], warnings
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

        topo_nodes, topo_warnings, topo_applied = self._filter_nodes_by_storage_topology(
            manifest, eligible
        )
        warnings.extend(topo_warnings)
        if topo_applied:
            eligible = topo_nodes

        if not eligible:
            warnings.append("no eligible nodes after storage constraints; skipping placement")
            if stale_nodes:
                warnings.append(f"stale/not-ready nodes skipped: {', '.join(stale_nodes)}")
            return [], warnings

        # Storage pinning: keep all replicas on one node when storage is declared or
        # when local-path PVCs require node-local provisioning.
        bound_node_id = None
        has_declared_storage = bool(getattr(manifest.spec, "storage", None))
        if has_declared_storage:
            try:
                attachments = self._store.list_volume_attachments(app_name)
                if attachments:
                    bound_node_id = attachments[0].node_id
            except Exception:
                bound_node_id = None

        local_pvc_pinning, local_bound_node, local_warnings = self._local_path_pinning(manifest)
        warnings.extend(local_warnings)
        if bound_node_id and local_bound_node and bound_node_id != local_bound_node:
            warnings.append(
                "storage attachments and single-writer PVCs disagree on bound node; "
                f"using {bound_node_id}"
            )
        if bound_node_id is None:
            bound_node_id = local_bound_node

        if has_declared_storage or local_pvc_pinning:
            if bound_node_id:
                bound_node = next((n for n in eligible if n.node_id == bound_node_id), None)
                if bound_node is not None:
                    if stale_nodes:
                        warnings.append(f"stale/not-ready nodes skipped: {', '.join(stale_nodes)}")
                    return [
                        Placement(
                            node=bound_node,
                            agent_url=bound_node.endpoint,
                            pod_names=pod_names,
                        )
                    ], warnings
                warnings.append(
                    f"storage volumes bound to node {bound_node_id} but node is not eligible (cordoned/not ready); skipping placement"
                )
                if stale_nodes:
                    warnings.append(f"stale/not-ready nodes skipped: {', '.join(stale_nodes)}")
                return [], warnings

            target = eligible[0]
            if local_pvc_pinning and len(eligible) > 1:
                warnings.append(
                    f"single-writer PVCs pending; pinning to node {target.node_id} until selected-node is set"
                )
            if stale_nodes:
                warnings.append(f"stale/not-ready nodes skipped: {', '.join(stale_nodes)}")
            return [Placement(node=target, agent_url=target.endpoint, pod_names=pod_names)], warnings

        # Topology spread / soft anti-affinity: if topologySpreadConstraints specify a
        # topologyKey and we have >1 eligible node, distribute replicas to minimize skew
        # across that key. This is a lightweight best-effort implementation.
        spread_key = self._topology_key(manifest)
        if spread_key and len(eligible) > 1:
            placements = self._spread_by_topology(eligible, pod_names, spread_key)
            if placements:
                if stale_nodes:
                    warnings.append(f"stale/not-ready nodes skipped: {', '.join(stale_nodes)}")
                return placements, warnings

        # Round-robin placement across eligible nodes.
        assignments: dict[str, list[str]] = {n.node_id: [] for n in eligible}
        for idx, rid in enumerate(pod_names):
            node = eligible[idx % len(eligible)]
            assignments[node.node_id].append(rid)

        placements: list[Placement] = []
        for node in eligible:
            ids = assignments.get(node.node_id, [])
            if ids:
                placements.append(Placement(node=node, agent_url=node.endpoint, pod_names=ids))

        if stale_nodes:
            warnings.append(f"stale/not-ready nodes skipped: {', '.join(stale_nodes)}")

        return placements or [Placement(node=None, agent_url=None, pod_names=pod_names)], warnings

    def _not_ready_grace_seconds(self) -> int:
        try:
            return int(os.getenv("AE_NODE_NOTREADY_AFTER", "40") or 40)
        except Exception:
            return 40

    def _init_shim_store(self):
        try:
            from ae.apishim.store import ObjectStore
        except Exception:
            return None
        dsn = os.getenv("AE_APISHIM_DSN")
        db_env = os.getenv("AE_APISHIM_DB")
        db_path = Path(db_env or "state/apishim.db")
        if not dsn and not db_path.exists():
            return None
        try:
            return ObjectStore(dsn=dsn) if dsn else ObjectStore(db_path=db_path)
        except Exception:
            return None

    def _local_path_pinning(self, manifest: AppManifest) -> tuple[bool, str | None, list[str]]:
        pvc_mounts = list(getattr(manifest.spec, "pvc_mounts", []) or [])
        if not pvc_mounts or self._shim_store is None:
            return False, None, []

        pvcs = self._pvc_mount_refs(manifest)

        local_present = False
        selected_nodes: set[str] = set()
        for claim_name, ns in pvcs:
            pvc = self._shim_store.get(CORE_GROUP, CORE_VERSION, PVC_RESOURCE, ns, claim_name)
            if pvc is None:
                continue
            needs_pinning = False
            sc_name = self._pvc_storage_class_name(pvc)
            if not sc_name:
                sc_name = self._default_storage_class_name()
            if sc_name:
                sc = self._shim_store.get(SC_GROUP, SC_VERSION, SC_RESOURCE, None, sc_name)
                if sc is not None:
                    sc_spec = self._obj_spec(sc)
                    provisioner = str(sc_spec.get("provisioner") or "")
                    binding_mode = str(sc_spec.get("volumeBindingMode") or "")
                    if provisioner == LOCAL_PATH_PROVISIONER and binding_mode == WAIT_FOR_FIRST_CONSUMER:
                        needs_pinning = True

            pv_name = self._pvc_volume_name(pvc)
            if pv_name:
                pv = self._shim_store.get(CORE_GROUP, CORE_VERSION, PV_RESOURCE, None, pv_name)
                if pv is not None:
                    pv_spec = self._obj_spec(pv)
                    csi = pv_spec.get("csi") if isinstance(pv_spec, dict) else None
                    if isinstance(csi, dict) and self._is_single_writer(pv_spec):
                        needs_pinning = True

            if not needs_pinning:
                continue
            local_present = True
            node = self._pvc_selected_node(pvc)
            if node:
                selected_nodes.add(node)

        warnings: list[str] = []
        if not local_present:
            return False, None, warnings
        bound_node = next(iter(selected_nodes)) if selected_nodes else None
        if len(selected_nodes) > 1:
            warnings.append(
                "single-writer PVCs reference multiple selected nodes; scheduling may be unstable"
            )
        return True, bound_node, warnings

    @staticmethod
    def _obj_spec(obj: Any) -> dict[str, Any]:
        spec = getattr(obj, "spec", None)
        return spec if isinstance(spec, dict) else {}

    @staticmethod
    def _pvc_mount_refs(manifest: AppManifest) -> set[tuple[str, str]]:
        pvc_mounts = list(getattr(manifest.spec, "pvc_mounts", []) or [])
        if not pvc_mounts:
            return set()
        namespace = getattr(getattr(manifest, "metadata", None), "namespace", None) or "default"
        return {
            (str(pm.claim_name), str(getattr(pm, "namespace", None) or namespace))
            for pm in pvc_mounts
        }

    def _rwop_claims(self, manifest: AppManifest) -> list[str]:
        if self._shim_store is None:
            return []
        claims: list[str] = []
        for claim_name, ns in self._pvc_mount_refs(manifest):
            pvc = self._shim_store.get(CORE_GROUP, CORE_VERSION, PVC_RESOURCE, ns, claim_name)
            if pvc is None:
                continue
            modes = set(self._access_modes(pvc))
            pv_name = self._pvc_volume_name(pvc)
            if pv_name:
                pv = self._shim_store.get(CORE_GROUP, CORE_VERSION, PV_RESOURCE, None, pv_name)
                if pv is not None:
                    modes.update(self._access_modes(pv))
            if "ReadWriteOncePod" in modes:
                claims.append(f"{ns}/{claim_name}")
        return sorted(set(claims))

    def _filter_nodes_by_storage_topology(
        self, manifest: AppManifest, nodes: list[NodeRecord]
    ) -> tuple[list[NodeRecord], list[str], bool]:
        if self._shim_store is None or not nodes:
            return nodes, [], False
        pvcs = self._pvc_mount_refs(manifest)
        if not pvcs:
            return nodes, [], False

        scs: dict[str, Any] = {}
        for claim_name, ns in pvcs:
            pvc = self._shim_store.get(CORE_GROUP, CORE_VERSION, PVC_RESOURCE, ns, claim_name)
            if pvc is None:
                continue
            sc_name = self._pvc_storage_class_name(pvc) or self._default_storage_class_name()
            if not sc_name or sc_name in scs:
                continue
            sc = self._shim_store.get(SC_GROUP, SC_VERSION, SC_RESOURCE, None, sc_name)
            if sc is not None:
                scs[sc_name] = sc
        if not scs:
            return nodes, [], False

        warnings: list[str] = []
        constrained_sets: list[set[str]] = []
        constrained_classes: list[str] = []
        topo_key_sets: list[set[str]] = []
        topo_key_classes: list[str] = []
        for sc_name, sc in scs.items():
            sc_spec = self._obj_spec(sc)
            allowed = sc_spec.get("allowedTopologies")
            if allowed:
                constrained_classes.append(sc_name)
                allowed_nodes = {
                    n.node_id for n in nodes if self._node_matches_allowed_topologies(n, allowed)
                }
                if not allowed_nodes:
                    warnings.append(
                        f"storage class {sc_name} allowedTopologies matches no eligible nodes"
                    )
                    return [], warnings, True
                constrained_sets.append(allowed_nodes)

            topo_keys_raw = sc_spec.get("topologyKeys")
            topo_keys = [str(k) for k in topo_keys_raw if k] if isinstance(topo_keys_raw, list) else []
            if topo_keys:
                topo_key_classes.append(sc_name)
                topo_nodes = {
                    n.node_id for n in nodes if self._node_has_label_keys(n, topo_keys)
                }
                if not topo_nodes:
                    warnings.append(
                        f"storage class {sc_name} topologyKeys matches no eligible nodes"
                    )
                    return [], warnings, True
                topo_key_sets.append(topo_nodes)

        if not constrained_sets and not topo_key_sets:
            return nodes, warnings, False

        all_sets = constrained_sets + topo_key_sets
        allowed_ids = set.intersection(*all_sets)
        if not allowed_ids:
            warnings.append("storage topology constraints intersect to zero eligible nodes")
            return [], warnings, True

        filtered = [n for n in nodes if n.node_id in allowed_ids]
        if len(filtered) < len(nodes):
            if constrained_sets:
                warnings.append(
                    "filtered eligible nodes by storage allowedTopologies for classes: "
                    + ", ".join(sorted(constrained_classes))
                )
            if topo_key_sets:
                warnings.append(
                    "filtered eligible nodes by storage topologyKeys for classes: "
                    + ", ".join(sorted(topo_key_classes))
                )
        return filtered, warnings, True

    def _default_storage_class_name(self) -> str | None:
        if self._default_sc_name:
            return self._default_sc_name
        if self._shim_store is None:
            return None
        try:
            classes = self._shim_store.list_all(SC_GROUP, SC_VERSION, SC_RESOURCE)
        except Exception:
            return None
        for sc in classes:
            meta = getattr(sc, "metadata", None)
            annotations = meta.get("annotations") if isinstance(meta, dict) else {}
            if not isinstance(annotations, dict):
                continue
            for key in DEFAULT_CLASS_ANNOTATIONS:
                raw = annotations.get(key)
                if raw is not None and str(raw).lower() in {"true", "1", "yes"}:
                    self._default_sc_name = sc.name
                    return self._default_sc_name
        if classes:
            self._default_sc_name = classes[0].name
            return self._default_sc_name
        return None

    @staticmethod
    def _pvc_storage_class_name(pvc) -> str | None:
        spec = getattr(pvc, "spec", None)
        if not isinstance(spec, dict):
            return None
        name = spec.get("storageClassName")
        return str(name) if name else None

    @staticmethod
    def _pvc_volume_name(pvc) -> str | None:
        spec = getattr(pvc, "spec", None)
        if not isinstance(spec, dict):
            return None
        name = spec.get("volumeName")
        return str(name) if name else None

    @staticmethod
    def _pvc_selected_node(pvc) -> str | None:
        meta = getattr(pvc, "metadata", None)
        annotations = meta.get("annotations") if isinstance(meta, dict) else {}
        if not isinstance(annotations, dict):
            return None
        node = annotations.get(SELECTED_NODE_ANNOTATION)
        return str(node) if node else None

    @staticmethod
    def _is_single_writer(spec: dict[str, Any]) -> bool:
        modes = set(spec.get("accessModes") or []) if isinstance(spec, dict) else set()
        return not bool(modes & {"ReadWriteMany", "ReadOnlyMany"})

    @staticmethod
    def _node_matches_allowed_topologies(node: NodeRecord, allowed: Any) -> bool:
        if not isinstance(allowed, list):
            return True
        labels = node.labels or {}
        if not labels:
            return False
        for term in allowed:
            exprs = term.get("matchLabelExpressions") if isinstance(term, dict) else None
            if not isinstance(exprs, list) or not exprs:
                continue
            if all(Scheduler._match_topology_expr(labels, expr) for expr in exprs):
                return True
        return False

    @staticmethod
    def _match_topology_expr(labels: dict[str, Any], expr: Any) -> bool:
        if not isinstance(expr, dict):
            return False
        key = expr.get("key")
        if not key:
            return False
        node_val = labels.get(str(key))
        if node_val is None:
            return False
        values = expr.get("values")
        if isinstance(values, list) and values:
            allowed_vals = {str(v) for v in values}
            return str(node_val) in allowed_vals
        return True

    @staticmethod
    def _node_has_label_keys(node: NodeRecord, keys: list[str]) -> bool:
        labels = node.labels or {}
        if not labels:
            return False
        return all(str(k) in labels for k in keys)

    @staticmethod
    def _access_modes(obj: Any) -> list[str]:
        spec = getattr(obj, "spec", None)
        if not isinstance(spec, dict):
            return []
        modes = spec.get("accessModes")
        if not isinstance(modes, list):
            return []
        return [str(m) for m in modes if m]

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
        nodes: list[NodeRecord], pod_names: list[str], topology_key: str
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

        for pod_name in pod_names:
            # choose topology value with the smallest current count (ties by key)
            val = sorted(counts.items(), key=lambda kv: (kv[1], kv[0]))[0][0]
            bucket = nodes_by_val[val]
            idx = node_index[val] % len(bucket)
            node_index[val] += 1
            counts[val] += 1
            target = bucket[idx]
            assignments[target.node_id].append(pod_name)

        placements: list[Placement] = []
        for n in nodes:
            ids = assignments.get(n.node_id, [])
            if ids:
                placements.append(Placement(node=n, agent_url=n.endpoint, pod_names=ids))
        return placements


# ruff: noqa
