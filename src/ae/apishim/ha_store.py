"""HA-mode apishim store routing onto shared controller authority."""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from http import HTTPStatus
from typing import Any

from ae.apishim.store import K8sObject, ObjectStore
from ae.controller.spec import AppManifest, ServiceSpec, app_key, app_key_for_manifest
from ae.controller.state import AuthorityObjectEntry, RegistryEntry, SQLiteStateStore
from ae.k8s import convert as k8s_convert
from ae.k8s.exporter import (
    ExportOptions,
    _deployment_from_manifest,
    _ingress_from_manifest,
    _job_from_manifest,
    _service_from_manifest,
    _statefulset_from_manifest,
)

WORKLOAD_KIND_LABEL = "apishim.k1s.dev/workload-kind"
SERVICE_NAME_LABEL = "apishim.k1s.dev/service-name"
SERVICE_CLUSTER_IP_LABEL = "apishim.k1s.dev/service-cluster-ip"
INGRESS_NAME_LABEL = "apishim.k1s.dev/ingress-name"
OWNER_API_VERSION_LABEL = "apishim.k1s.dev/owner-api-version"
OWNER_KIND_LABEL = "apishim.k1s.dev/owner-kind"
OWNER_NAME_LABEL = "apishim.k1s.dev/owner-name"
OWNER_UID_LABEL = "apishim.k1s.dev/owner-uid"
CRONJOB_SCHEDULED_AT_LABEL = "apishim.k1s.dev/cronjob-scheduled-at"

WORKLOAD_RESOURCES: set[tuple[str, str, str]] = {
    ("apps", "v1", "deployments"),
    ("apps", "v1", "statefulsets"),
    ("apps", "v1", "daemonsets"),
    ("batch", "v1", "jobs"),
}
ATTACHED_RESOURCES: set[tuple[str, str, str]] = {
    ("", "v1", "services"),
    ("networking.k8s.io", "v1", "ingresses"),
}
GENERIC_AUTHORITY_RESOURCES: set[tuple[str, str, str]] = {
    ("", "v1", "namespaces"),
    ("", "v1", "configmaps"),
    ("", "v1", "secrets"),
    ("", "v1", "serviceaccounts"),
    ("batch", "v1", "cronjobs"),
    ("autoscaling", "v2", "horizontalpodautoscalers"),
    ("rbac.authorization.k8s.io", "v1", "roles"),
    ("rbac.authorization.k8s.io", "v1", "rolebindings"),
    ("rbac.authorization.k8s.io", "v1", "clusterroles"),
    ("rbac.authorization.k8s.io", "v1", "clusterrolebindings"),
    ("policy", "v1", "poddisruptionbudgets"),
}
WORKLOAD_AUTHORITY_RESOURCES = WORKLOAD_RESOURCES | ATTACHED_RESOURCES
AUTHORITY_RESOURCES = WORKLOAD_AUTHORITY_RESOURCES | GENERIC_AUTHORITY_RESOURCES


class AuthorityMutationError(RuntimeError):
    """Raised when an HA workload-core mutation cannot be mapped safely."""

    def __init__(
        self,
        *,
        status_code: int = int(HTTPStatus.UNPROCESSABLE_ENTITY),
        reason: str = "Invalid",
        message: str,
    ) -> None:
        super().__init__(message)
        self.status_code = int(status_code)
        self.reason = str(reason)
        self.message = str(message)


def is_authority_resource(group: str, version: str, resource: str) -> bool:
    return (group, version, resource) in AUTHORITY_RESOURCES


def is_workload_authority_resource(group: str, version: str, resource: str) -> bool:
    return (group, version, resource) in WORKLOAD_AUTHORITY_RESOURCES


def is_generic_authority_resource(group: str, version: str, resource: str) -> bool:
    return (group, version, resource) in GENERIC_AUTHORITY_RESOURCES


def generic_kind_for_resource(resource: str) -> str:
    return {
        "namespaces": "Namespace",
        "configmaps": "ConfigMap",
        "secrets": "Secret",
        "serviceaccounts": "ServiceAccount",
        "cronjobs": "CronJob",
        "horizontalpodautoscalers": "HorizontalPodAutoscaler",
        "roles": "Role",
        "rolebindings": "RoleBinding",
        "clusterroles": "ClusterRole",
        "clusterrolebindings": "ClusterRoleBinding",
        "poddisruptionbudgets": "PodDisruptionBudget",
    }.get(resource, resource[:-1].capitalize())


_HPA_TARGET_RESOURCES: dict[str, tuple[str, str, str]] = {
    "deployment": ("apps", "v1", "deployments"),
    "statefulset": ("apps", "v1", "statefulsets"),
    "daemonset": ("apps", "v1", "daemonsets"),
}


def _validate_hpa_spec(spec: dict[str, Any]) -> None:
    normalized = spec
    if isinstance(normalized, dict) and isinstance(normalized.get("spec"), dict):
        normalized = normalized["spec"]
    target = normalized.get("scaleTargetRef") if isinstance(normalized, dict) else None
    if not isinstance(target, dict):
        raise AuthorityMutationError(message="HPA spec.scaleTargetRef is required in HA mode")
    target_kind = str(target.get("kind") or "").strip().lower()
    if target_kind not in _HPA_TARGET_RESOURCES:
        raise AuthorityMutationError(
            message=(
                "HA HPA supports only Deployment, StatefulSet, and DaemonSet targets"
            )
        )
    metrics = normalized.get("metrics")
    if not isinstance(metrics, list) or not metrics:
        raise AuthorityMutationError(message="HPA spec.metrics must contain at least one resource metric")
    for metric in metrics:
        if not isinstance(metric, dict):
            raise AuthorityMutationError(message="HPA metrics entries must be objects")
        if str(metric.get("type") or "").strip() != "Resource":
            raise AuthorityMutationError(
                message="HA HPA supports only Resource metrics"
            )
        resource_cfg = metric.get("resource")
        if not isinstance(resource_cfg, dict):
            raise AuthorityMutationError(message="HPA resource metric config is required")
        resource_name = str(resource_cfg.get("name") or "").strip().lower()
        if resource_name not in {"cpu", "memory"}:
            raise AuthorityMutationError(
                message="HA HPA supports only cpu and memory resource metrics"
            )
        target_cfg = resource_cfg.get("target")
        if not isinstance(target_cfg, dict):
            raise AuthorityMutationError(message="HPA resource metric target is required")
        target_type = str(target_cfg.get("type") or "").strip()
        if resource_name == "memory":
            if target_type not in {"Utilization", "AverageValue"}:
                raise AuthorityMutationError(
                    message=(
                        "HA HPA memory metrics support only Utilization and AverageValue targets"
                    )
                )
        elif target_type != "Utilization":
            raise AuthorityMutationError(
                message="HA HPA cpu metrics support only Utilization targets"
            )


def workload_kind_for_entry(entry: RegistryEntry) -> str:
    labels = entry.labels or {}
    raw = str(labels.get(WORKLOAD_KIND_LABEL) or "").strip().lower()
    if raw in {"deployment", "statefulset", "daemonset", "job"}:
        return raw
    workload = str(getattr(entry.manifest.spec, "workload", "service") or "service").lower()
    if workload == "job":
        return "job"
    return "deployment"


def workload_resource_for_entry(entry: RegistryEntry) -> tuple[str, str, str]:
    kind = workload_kind_for_entry(entry)
    if kind == "statefulset":
        return ("apps", "v1", "statefulsets")
    if kind == "daemonset":
        return ("apps", "v1", "daemonsets")
    if kind == "job":
        return ("batch", "v1", "jobs")
    return ("apps", "v1", "deployments")


def daemonset_manifest_for_entry(entry: RegistryEntry, state: SQLiteStateStore) -> AppManifest:
    manifest = entry.manifest
    try:
        desired = max(1, len(state.list_nodes()))
    except Exception:
        desired = max(1, int(getattr(manifest.spec, "replicas", 1) or 1))
    return manifest.model_copy(update={"spec": manifest.spec.model_copy(update={"replicas": desired})})


def materialize_registry_manifests(
    store: SQLiteStateStore, entries: list[RegistryEntry]
) -> list[AppManifest]:
    manifests: list[AppManifest] = []
    for entry in entries:
        if workload_kind_for_entry(entry) == "daemonset":
            manifests.append(daemonset_manifest_for_entry(entry, store))
            continue
        manifests.append(entry.manifest)
    return manifests


def _registry_labels(
    entry: RegistryEntry | None, *, internal_updates: dict[str, str | None] | None = None
) -> dict[str, str]:
    labels = {
        str(key): str(val)
        for key, val in ((entry.labels or {}) if entry is not None else {}).items()
        if val is not None
    }
    if internal_updates:
        for key, value in internal_updates.items():
            if value is None:
                labels.pop(key, None)
            else:
                labels[key] = str(value)
    return labels


def _rv_from_metadata(metadata: dict[str, Any]) -> int | None:
    raw = metadata.get("resourceVersion")
    if raw in {None, ""}:
        return None
    try:
        return int(raw)
    except Exception as exc:  # noqa: BLE001
        raise AuthorityMutationError(message="metadata.resourceVersion must be an integer") from exc


def _service_name(entry: RegistryEntry) -> str:
    raw = str((entry.labels or {}).get(SERVICE_NAME_LABEL) or "").strip()
    if raw:
        return raw
    return entry.manifest.metadata.name


def _ingress_name(entry: RegistryEntry) -> str:
    raw = str((entry.labels or {}).get(INGRESS_NAME_LABEL) or "").strip()
    if raw:
        return raw
    return entry.manifest.metadata.name


def _service_cluster_ip(entry: RegistryEntry, state: SQLiteStateStore) -> str | None:
    try:
        rec = state.get_service(entry.app_name)
        if rec and rec.cluster_ip:
            return str(rec.cluster_ip)
    except Exception:
        pass
    raw = str((entry.labels or {}).get(SERVICE_CLUSTER_IP_LABEL) or "").strip()
    return raw or None


def _owner_references_for_entry(entry: RegistryEntry) -> list[dict[str, Any]]:
    labels = entry.labels or {}
    owner_kind = str(labels.get(OWNER_KIND_LABEL) or "").strip()
    owner_name = str(labels.get(OWNER_NAME_LABEL) or "").strip()
    owner_uid = str(labels.get(OWNER_UID_LABEL) or "").strip()
    if not owner_kind or not owner_name or not owner_uid:
        return []
    return [
        {
            "apiVersion": str(labels.get(OWNER_API_VERSION_LABEL) or "v1"),
            "kind": owner_kind,
            "name": owner_name,
            "uid": owner_uid,
            "controller": True,
            "blockOwnerDeletion": True,
        }
    ]


def _owner_label_updates(metadata: dict[str, Any] | None) -> dict[str, str]:
    refs = list((metadata or {}).get("ownerReferences") or [])
    if not refs:
        return {}
    ref = refs[0] if isinstance(refs[0], dict) else {}
    owner_kind = str(ref.get("kind") or "").strip()
    owner_name = str(ref.get("name") or "").strip()
    owner_uid = str(ref.get("uid") or "").strip()
    if not owner_kind or not owner_name or not owner_uid:
        return {}
    return {
        OWNER_API_VERSION_LABEL: str(ref.get("apiVersion") or "v1"),
        OWNER_KIND_LABEL: owner_kind,
        OWNER_NAME_LABEL: owner_name,
        OWNER_UID_LABEL: owner_uid,
    }


def _workload_doc(entry: RegistryEntry, state: SQLiteStateStore) -> tuple[str, dict[str, Any]]:
    kind = workload_kind_for_entry(entry)
    manifest = daemonset_manifest_for_entry(entry, state) if kind == "daemonset" else entry.manifest
    opts = ExportOptions(
        namespace=getattr(manifest.metadata, "namespace", None),
        workload_kind="StatefulSet" if kind == "statefulset" else ("Job" if kind == "job" else "Deployment"),
        emit_configs=False,
        emit_secrets=False,
        emit_storage=False,
        emit_pdb=False,
        emit_network_policy=False,
        emit_namespace=False,
    )
    if kind == "statefulset":
        doc = _statefulset_from_manifest(manifest, opts)
    elif kind == "job":
        doc = _job_from_manifest(manifest, opts)
    else:
        doc = _deployment_from_manifest(manifest, opts)
        if kind == "daemonset":
            doc = {
                "apiVersion": "apps/v1",
                "kind": "DaemonSet",
                "metadata": dict(doc.get("metadata") or {}),
                "spec": dict(doc.get("spec") or {}),
            }
    metadata = dict(doc.get("metadata") or {})
    metadata["resourceVersion"] = str(entry.resource_version)
    metadata.setdefault("generation", int(entry.resource_version or 1))
    owner_references = _owner_references_for_entry(entry)
    if owner_references:
        metadata["ownerReferences"] = owner_references
    doc["metadata"] = metadata
    return kind, doc


def _workload_status(entry: RegistryEntry, state: SQLiteStateStore) -> dict[str, Any]:
    try:
        row = state.get_status(entry.app_name)
    except Exception:
        row = None
    if row is None:
        return {}
    kind = workload_kind_for_entry(entry)
    if kind == "job":
        active = 0
        succeeded = 0
        failed = 0
        try:
            pods = state.list_pods(entry.app_name)
        except Exception:
            pods = []
        for pod in pods:
            if getattr(pod, "status", "") == "running":
                active += 1
            exit_code = getattr(pod, "exit_code", None)
            if exit_code is None:
                continue
            if int(exit_code) == 0:
                succeeded += 1
            else:
                failed += 1
        return {
            "active": active,
            "succeeded": succeeded,
            "failed": failed,
            "observedGeneration": int(entry.resource_version or 1),
        }
    if kind == "daemonset":
        try:
            desired = max(1, len(state.list_nodes()))
        except Exception:
            desired = max(1, int(getattr(entry.manifest.spec, "replicas", 1) or 1))
        return {
            "desiredNumberScheduled": desired,
            "currentNumberScheduled": int(getattr(row, "live_replicas", 0) or 0),
            "numberReady": int(getattr(row, "ready_replicas", 0) or 0),
            "numberAvailable": int(getattr(row, "ready_replicas", 0) or 0),
            "updatedNumberScheduled": int(getattr(row, "live_replicas", 0) or 0),
            "observedGeneration": int(entry.resource_version or 1),
        }
    return {
        "replicas": int(getattr(row, "desired_replicas", 0) or 0),
        "updatedReplicas": int(getattr(row, "live_replicas", 0) or 0),
        "readyReplicas": int(getattr(row, "ready_replicas", 0) or 0),
        "availableReplicas": int(getattr(row, "ready_replicas", 0) or 0),
        "currentReplicas": int(getattr(row, "live_replicas", 0) or 0),
        "observedGeneration": int(entry.resource_version or 1),
    }


def _k8s_object_from_doc(
    group: str, version: str, resource: str, doc: dict[str, Any], *, status: dict[str, Any] | None = None
) -> K8sObject:
    metadata = dict(doc.get("metadata") or {})
    namespace = metadata.get("namespace")
    name = str(metadata.get("name") or "")
    rv = metadata.get("resourceVersion") or 0
    spec = dict(doc.get("spec") or {})
    resolved_status = status if status is not None else dict(doc.get("status") or {})
    return K8sObject(
        group,
        version,
        resource,
        str(namespace) if namespace not in {None, ""} else None,
        name,
        metadata,
        spec,
        resolved_status,
        int(rv or 0),
    )


def _manifest_port_map(manifest: AppManifest) -> dict[str, int]:
    ports: dict[str, int] = {}
    for port in list(getattr(manifest.spec, "ports", []) or []):
        try:
            if getattr(port, "name", None):
                ports[str(port.name)] = int(port.container_port)
        except Exception:
            continue
    for container in list(getattr(manifest.spec, "containers", []) or []):
        for port in list(getattr(container, "ports", []) or []):
            try:
                if getattr(port, "name", None):
                    ports[str(port.name)] = int(port.container_port)
            except Exception:
                continue
    return ports


def _service_spec_from_object(obj: K8sObject, manifest: AppManifest) -> ServiceSpec:
    spec = obj.spec or {}
    ports_by_name = _manifest_port_map(manifest)
    svc_ports: list[ServiceSpec.ServicePort] = []
    for idx, entry in enumerate(spec.get("ports") or []):
        if not isinstance(entry, dict):
            continue
        try:
            svc_port = int(entry.get("port"))
        except Exception:
            continue
        target_raw = entry.get("targetPort", svc_port)
        target = k8s_convert.resolve_port_value(target_raw, ports_by_name)
        if target is None:
            try:
                target = int(target_raw)
            except Exception:
                target = svc_port
        node_port = entry.get("nodePort")
        try:
            node_port_val = int(node_port) if node_port is not None else None
        except Exception:
            node_port_val = None
        svc_ports.append(
            ServiceSpec.ServicePort(
                name=str(entry.get("name") or f"port-{idx}"),
                port=svc_port,
                targetPort=target,
                protocol=str(entry.get("protocol") or "TCP"),
                nodePort=node_port_val,
            )
        )
    cfg = None
    sac = spec.get("sessionAffinityConfig") or {}
    client_ip = sac.get("clientIP") if isinstance(sac, dict) else None
    if isinstance(client_ip, dict) and client_ip.get("timeoutSeconds") is not None:
        cfg = ServiceSpec.SessionAffinityConfig(
            clientIP=ServiceSpec.SessionAffinityClientIP(
                timeoutSeconds=int(client_ip.get("timeoutSeconds"))
            )
        )
    return ServiceSpec(
        type=spec.get("type") or None,
        externalTrafficPolicy=spec.get("externalTrafficPolicy") or None,
        port=(svc_ports[0].port if svc_ports else None),
        targetPort=(svc_ports[0].target_port if svc_ports else None),
        ports=svc_ports,
        externalIPs=list(spec.get("externalIPs") or []),
        sessionAffinity=spec.get("sessionAffinity") or None,
        sessionAffinityConfig=cfg,
    )


def _service_doc(entry: RegistryEntry, state: SQLiteStateStore) -> dict[str, Any] | None:
    if getattr(entry.manifest.spec, "service", None) is None:
        return None
    doc = _service_from_manifest(
        entry.manifest,
        ExportOptions(
            namespace=getattr(entry.manifest.metadata, "namespace", None),
            emit_storage=False,
            emit_configs=False,
            emit_secrets=False,
            emit_pdb=False,
            emit_network_policy=False,
        ),
    )
    if doc is None:
        return None
    metadata = dict(doc.get("metadata") or {})
    metadata["name"] = _service_name(entry)
    metadata["resourceVersion"] = str(entry.resource_version)
    metadata.setdefault("generation", int(entry.resource_version or 1))
    doc["metadata"] = metadata
    spec = dict(doc.get("spec") or {})
    cluster_ip = _service_cluster_ip(entry, state)
    if cluster_ip:
        spec["clusterIP"] = cluster_ip
        spec["clusterIPs"] = [cluster_ip]
    doc["spec"] = spec
    return doc


def _ingress_doc(entry: RegistryEntry) -> dict[str, Any] | None:
    if getattr(entry.manifest.spec, "ingress", None) is None:
        return None
    doc = _ingress_from_manifest(
        entry.manifest,
        ExportOptions(
            namespace=getattr(entry.manifest.metadata, "namespace", None),
            emit_storage=False,
            emit_configs=False,
            emit_secrets=False,
            emit_pdb=False,
            emit_network_policy=False,
        ),
    )
    if doc is None:
        return None
    metadata = dict(doc.get("metadata") or {})
    metadata["name"] = _ingress_name(entry)
    metadata["resourceVersion"] = str(entry.resource_version)
    metadata.setdefault("generation", int(entry.resource_version or 1))
    doc["metadata"] = metadata
    service_name = _service_name(entry)
    spec = dict(doc.get("spec") or {})
    if spec.get("defaultBackend", {}).get("service"):
        spec["defaultBackend"]["service"]["name"] = service_name
    for rule in spec.get("rules") or []:
        http = rule.get("http") or {}
        for path in http.get("paths") or []:
            backend = path.get("backend", {}).get("service")
            if isinstance(backend, dict):
                backend["name"] = service_name
    doc["spec"] = spec
    return doc


def _entry_to_object(entry: RegistryEntry, resource: str, state: SQLiteStateStore) -> K8sObject | None:
    if resource == "services":
        doc = _service_doc(entry, state)
        if doc is None:
            return None
        return _k8s_object_from_doc("", "v1", "services", doc)
    if resource == "ingresses":
        doc = _ingress_doc(entry)
        if doc is None:
            return None
        return _k8s_object_from_doc("networking.k8s.io", "v1", "ingresses", doc)
    group, version, resolved_resource = workload_resource_for_entry(entry)
    if resolved_resource != resource:
        return None
    kind, doc = _workload_doc(entry, state)
    status = _workload_status(entry, state)
    return _k8s_object_from_doc(group, version, resolved_resource, doc, status=status)


def _authority_object_to_k8s(entry: AuthorityObjectEntry) -> K8sObject:
    metadata = dict(entry.metadata or {})
    metadata.setdefault("name", entry.name)
    if entry.namespace:
        metadata.setdefault("namespace", entry.namespace)
    metadata["resourceVersion"] = str(entry.resource_version)
    return K8sObject(
        entry.group,
        entry.version,
        entry.resource,
        entry.namespace,
        entry.name,
        metadata,
        dict(entry.spec or {}),
        dict(entry.status or {}),
        int(entry.resource_version or 0),
    )


class WorkloadAuthorityStore:
    """Store adapter exposing converged HA workload resources via controller state."""

    def __init__(self, state: SQLiteStateStore) -> None:
        self._state = state
        self.backend = "ha-controller"
        self._watch_poll_interval = max(
            0.1, float(os.getenv("AE_APISHIM_HA_WATCH_POLL_SEC", "0.5") or "0.5")
        )

    def close(self) -> None:
        return None

    def export_all(self):
        return iter(())

    def render_metrics(self) -> str:
        return (
            "# HELP apishim_store_backend_info Backend in use for shim object store\n"
            "# TYPE apishim_store_backend_info gauge\n"
            'apishim_store_backend_info{backend="ha-controller"} 1\n'
        )

    def _entries(self) -> list[RegistryEntry]:
        try:
            entries = self._state.list_registered_apps()
        except Exception:
            return []
        entries.sort(
            key=lambda entry: (
                getattr(entry.manifest.metadata, "namespace", None) or "",
                entry.manifest.metadata.name,
            )
        )
        return entries

    def _find_workload_entry(self, namespace: str | None, name: str) -> RegistryEntry | None:
        app_name = app_key(name, namespace)
        try:
            return self._state.get_registered_entry(app_name)
        except Exception:
            return None

    def _find_attached_entry(
        self, label_key: str, namespace: str | None, resource_name: str
    ) -> RegistryEntry | None:
        for entry in self._entries():
            entry_ns = getattr(entry.manifest.metadata, "namespace", None) or "default"
            if (namespace or "default") != entry_ns:
                continue
            if str((entry.labels or {}).get(label_key) or "") == str(resource_name):
                return entry
        return None

    def _workload_objects(
        self, group: str, version: str, resource: str, namespace: str | None
    ) -> list[K8sObject]:
        out: list[K8sObject] = []
        for entry in self._entries():
            entry_ns = getattr(entry.manifest.metadata, "namespace", None)
            if namespace is not None and (entry_ns or "default") != (namespace or "default"):
                continue
            obj = _entry_to_object(entry, resource, self._state)
            if obj is None:
                continue
            if obj.group == group and obj.version == version and obj.resource == resource:
                out.append(obj)
        out.sort(key=lambda obj: (obj.namespace or "", obj.name))
        return out

    def _service_objects(self, namespace: str | None) -> list[K8sObject]:
        return self._workload_objects("", "v1", "services", namespace)

    def _ingress_objects(self, namespace: str | None) -> list[K8sObject]:
        return self._workload_objects("networking.k8s.io", "v1", "ingresses", namespace)

    def get(
        self, group: str, version: str, resource: str, namespace: str | None, name: str
    ) -> K8sObject | None:
        if not is_workload_authority_resource(group, version, resource):
            return None
        if resource == "services":
            entry = self._find_attached_entry(SERVICE_NAME_LABEL, namespace, name)
            return _entry_to_object(entry, "services", self._state) if entry is not None else None
        if resource == "ingresses":
            entry = self._find_attached_entry(INGRESS_NAME_LABEL, namespace, name)
            return _entry_to_object(entry, "ingresses", self._state) if entry is not None else None
        entry = self._find_workload_entry(namespace, name)
        if entry is None:
            return None
        obj = _entry_to_object(entry, resource, self._state)
        if obj is None:
            return None
        if obj.group != group or obj.version != version or obj.resource != resource:
            return None
        return obj

    def list(
        self, group: str, version: str, resource: str, namespace: str | None | None
    ) -> list[K8sObject]:
        if not is_workload_authority_resource(group, version, resource):
            return []
        if resource == "services":
            return self._service_objects(namespace)
        if resource == "ingresses":
            return self._ingress_objects(namespace)
        return self._workload_objects(group, version, resource, namespace)

    def list_all(self, group: str, version: str, resource: str) -> list[K8sObject]:
        return self.list(group, version, resource, None)

    def _service_target_entry(self, obj: K8sObject) -> RegistryEntry:
        existing = self._find_attached_entry(SERVICE_NAME_LABEL, obj.namespace, obj.name)
        if existing is not None:
            return existing
        selector = k8s_convert.service_selector(obj.spec or {})
        candidates: list[RegistryEntry] = []
        for entry in self._entries():
            if workload_kind_for_entry(entry) in {"job"}:
                continue
            entry_ns = getattr(entry.manifest.metadata, "namespace", None) or "default"
            if entry_ns != (obj.namespace or "default"):
                continue
            workload_obj = _entry_to_object(
                entry,
                workload_resource_for_entry(entry)[2],
                self._state,
            )
            if workload_obj is None:
                continue
            labels = k8s_convert.pod_template_labels(workload_obj)
            if selector and labels and k8s_convert.selector_matches(selector, labels):
                candidates.append(entry)
                continue
            if not selector and entry.manifest.metadata.name == obj.name:
                candidates.append(entry)
        if not candidates:
            raise AuthorityMutationError(
                message="service selector must resolve to one converged workload in the same namespace"
            )
        unique = {candidate.app_name: candidate for candidate in candidates}
        if len(unique) != 1:
            raise AuthorityMutationError(
                message="service selector must resolve unambiguously to one converged workload"
            )
        return next(iter(unique.values()))

    def _ingress_target_entry(self, obj: K8sObject) -> RegistryEntry:
        existing = self._find_attached_entry(INGRESS_NAME_LABEL, obj.namespace, obj.name)
        if existing is not None:
            return existing
        service_targets: set[str] = set()
        spec = obj.spec or {}
        default_backend = (spec.get("defaultBackend") or {}).get("service") or {}
        if default_backend.get("name"):
            service_targets.add(str(default_backend.get("name")))
        for rule in spec.get("rules") or []:
            http = rule.get("http") or {}
            for path in http.get("paths") or []:
                backend = (path.get("backend") or {}).get("service") or {}
                if backend.get("name"):
                    service_targets.add(str(backend.get("name")))
        if not service_targets:
            raise AuthorityMutationError(message="ingress must reference a converged service backend")
        entries: dict[str, RegistryEntry] = {}
        for svc_name in service_targets:
            entry = self._find_attached_entry(SERVICE_NAME_LABEL, obj.namespace, svc_name)
            if entry is not None:
                entries[entry.app_name] = entry
        if len(entries) != 1:
            raise AuthorityMutationError(
                message="ingress backends must resolve to exactly one converged workload service"
            )
        return next(iter(entries.values()))

    def _register_workload(
        self, obj: K8sObject, *, entry: RegistryEntry | None = None
    ) -> K8sObject:
        metadata = obj.metadata or {}
        resource = obj.resource
        internal_updates: dict[str, str | None] = {WORKLOAD_KIND_LABEL: resource[:-1]}
        manifest: AppManifest
        if resource == "jobs":
            manifest = k8s_convert.manifest_from_k8s_workload(obj)
            job_labels = dict(getattr(manifest.metadata, "labels", None) or {})
            job_labels.setdefault("ae.workload", "job")
            internal_updates.update(_owner_label_updates(metadata))
            updates = {
                "workload": "job",
                "jobBackoffLimit": (obj.spec or {}).get("backoffLimit"),
                "jobTtlSecondsAfterFinished": (obj.spec or {}).get("ttlSecondsAfterFinished"),
            }
            manifest = manifest.model_copy(
                update={
                    "metadata": manifest.metadata.model_copy(update={"labels": job_labels}),
                    "spec": manifest.spec.model_copy(
                        update={key: value for key, value in updates.items() if value is not None}
                    ),
                }
            )
        elif resource == "statefulsets":
            manifest = k8s_convert.manifest_from_k8s_workload(
                obj,
                volume_claim_templates=(obj.spec or {}).get("volumeClaimTemplates"),
            )
        else:
            manifest = k8s_convert.manifest_from_k8s_workload(obj)
            if resource == "daemonsets":
                try:
                    desired = max(1, len(self._state.list_nodes()))
                except Exception:
                    desired = 1
                manifest = manifest.model_copy(
                    update={"spec": manifest.spec.model_copy(update={"replicas": desired})}
                )
        if entry is not None:
            if getattr(entry.manifest.spec, "service", None) is not None:
                manifest = manifest.model_copy(
                    update={"spec": manifest.spec.model_copy(update={"service": entry.manifest.spec.service})}
                )
                internal_updates[SERVICE_NAME_LABEL] = str((entry.labels or {}).get(SERVICE_NAME_LABEL) or entry.manifest.metadata.name)
                internal_updates[SERVICE_CLUSTER_IP_LABEL] = str((entry.labels or {}).get(SERVICE_CLUSTER_IP_LABEL) or "")
            if getattr(entry.manifest.spec, "ingress", None) is not None:
                manifest = manifest.model_copy(
                    update={"spec": manifest.spec.model_copy(update={"ingress": entry.manifest.spec.ingress})}
                )
                internal_updates[INGRESS_NAME_LABEL] = str((entry.labels or {}).get(INGRESS_NAME_LABEL) or entry.manifest.metadata.name)
        labels = _registry_labels(entry, internal_updates=internal_updates)
        expected_rv = _rv_from_metadata(metadata)
        if expected_rv is None:
            expected_rv = entry.resource_version if entry is not None else 0
        rv = self._state.register_app(
            manifest,
            source="apishim",
            labels=labels,
            expected_resource_version=expected_rv,
        )
        fresh = self._state.get_registered_entry(app_key_for_manifest(manifest))
        if fresh is None:
            raise AuthorityMutationError(
                status_code=int(HTTPStatus.INTERNAL_SERVER_ERROR),
                reason="InternalError",
                message="failed to read back workload authority entry",
            )
        return _entry_to_object(fresh, resource, self._state) or K8sObject(
            obj.group,
            obj.version,
            obj.resource,
            obj.namespace,
            obj.name,
            dict(metadata, resourceVersion=str(rv)),
            obj.spec,
            obj.status,
            rv,
        )

    def _register_service(self, obj: K8sObject) -> K8sObject:
        entry = self._service_target_entry(obj)
        service_spec = _service_spec_from_object(obj, entry.manifest)
        new_manifest = entry.manifest.model_copy(
            update={"spec": entry.manifest.spec.model_copy(update={"service": service_spec})}
        )
        labels = _registry_labels(
            entry,
            internal_updates={
                SERVICE_NAME_LABEL: obj.name,
                SERVICE_CLUSTER_IP_LABEL: str((obj.spec or {}).get("clusterIP") or ""),
            },
        )
        expected_rv = _rv_from_metadata(obj.metadata or {})
        if expected_rv is None:
            expected_rv = entry.resource_version
        self._state.register_app(
            new_manifest,
            source="apishim",
            labels=labels,
            expected_resource_version=expected_rv,
        )
        fresh = self._state.get_registered_entry(entry.app_name)
        if fresh is None:
            raise AuthorityMutationError(
                status_code=int(HTTPStatus.INTERNAL_SERVER_ERROR),
                reason="InternalError",
                message="failed to read back attached service entry",
            )
        attached = _entry_to_object(fresh, "services", self._state)
        if attached is None:
            raise AuthorityMutationError(
                status_code=int(HTTPStatus.INTERNAL_SERVER_ERROR),
                reason="InternalError",
                message="failed to synthesize attached service",
            )
        return attached

    def _register_ingress(self, obj: K8sObject) -> K8sObject:
        entry = self._ingress_target_entry(obj)
        service_name_map = {
            (getattr(entry.manifest.metadata, "namespace", None), _service_name(entry)): (
                getattr(entry.manifest.metadata, "namespace", None),
                entry.manifest.metadata.name,
            )
        }
        result = k8s_convert.ingress_spec_from_k8s(obj, service_name_map)
        if not result:
            raise AuthorityMutationError(message="ingress must resolve to one converged service backend")
        _target_key, ingress_spec = result
        if workload_kind_for_entry(entry) == "job":
            raise AuthorityMutationError(message="ingress cannot target a Job workload")
        new_manifest = entry.manifest.model_copy(
            update={"spec": entry.manifest.spec.model_copy(update={"ingress": ingress_spec})}
        )
        labels = _registry_labels(
            entry,
            internal_updates={INGRESS_NAME_LABEL: obj.name},
        )
        expected_rv = _rv_from_metadata(obj.metadata or {})
        if expected_rv is None:
            expected_rv = entry.resource_version
        self._state.register_app(
            new_manifest,
            source="apishim",
            labels=labels,
            expected_resource_version=expected_rv,
        )
        fresh = self._state.get_registered_entry(entry.app_name)
        if fresh is None:
            raise AuthorityMutationError(
                status_code=int(HTTPStatus.INTERNAL_SERVER_ERROR),
                reason="InternalError",
                message="failed to read back attached ingress entry",
            )
        attached = _entry_to_object(fresh, "ingresses", self._state)
        if attached is None:
            raise AuthorityMutationError(
                status_code=int(HTTPStatus.INTERNAL_SERVER_ERROR),
                reason="InternalError",
                message="failed to synthesize attached ingress",
            )
        return attached

    def upsert(
        self,
        group: str,
        version: str,
        resource: str,
        namespace: str | None,
        name: str,
        metadata: dict[str, Any],
        spec: dict[str, Any],
        status: dict[str, Any] | None = None,
        resource_version: int | None = None,
    ) -> K8sObject:
        if not is_workload_authority_resource(group, version, resource):
            raise AuthorityMutationError(message=f"unsupported authority resource {group}/{version}/{resource}")
        obj = K8sObject(
            group,
            version,
            resource,
            namespace,
            name,
            dict(metadata),
            dict(spec),
            dict(status or {}),
            int(resource_version or 0),
        )
        if resource == "services":
            return self._register_service(obj)
        if resource == "ingresses":
            return self._register_ingress(obj)
        entry = self._find_workload_entry(namespace, name)
        return self._register_workload(obj, entry=entry)

    def upsert_if_not_deleted(
        self,
        group: str,
        version: str,
        resource: str,
        namespace: str | None,
        name: str,
        metadata: dict[str, Any],
        spec: dict[str, Any],
        status: dict[str, Any] | None = None,
        resource_version: int | None = None,
    ) -> K8sObject | None:
        return self.upsert(
            group,
            version,
            resource,
            namespace,
            name,
            metadata,
            spec,
            status=status,
            resource_version=resource_version,
        )

    def delete(
        self, group: str, version: str, resource: str, namespace: str | None, name: str
    ) -> bool:
        if not is_workload_authority_resource(group, version, resource):
            return False
        if resource == "services":
            entry = self._find_attached_entry(SERVICE_NAME_LABEL, namespace, name)
            if entry is None or getattr(entry.manifest.spec, "service", None) is None:
                return False
            labels = _registry_labels(
                entry,
                internal_updates={SERVICE_NAME_LABEL: None, SERVICE_CLUSTER_IP_LABEL: None},
            )
            updated = entry.manifest.model_copy(
                update={"spec": entry.manifest.spec.model_copy(update={"service": None})}
            )
            self._state.register_app(
                updated,
                source="apishim",
                labels=labels,
                expected_resource_version=entry.resource_version,
            )
            return True
        if resource == "ingresses":
            entry = self._find_attached_entry(INGRESS_NAME_LABEL, namespace, name)
            if entry is None or getattr(entry.manifest.spec, "ingress", None) is None:
                return False
            labels = _registry_labels(entry, internal_updates={INGRESS_NAME_LABEL: None})
            updated = entry.manifest.model_copy(
                update={"spec": entry.manifest.spec.model_copy(update={"ingress": None})}
            )
            self._state.register_app(
                updated,
                source="apishim",
                labels=labels,
                expected_resource_version=entry.resource_version,
            )
            return True
        entry = self._find_workload_entry(namespace, name)
        if entry is None:
            return False
        return bool(
            self._state.delete_registered_app(
                entry.app_name,
                expected_resource_version=entry.resource_version,
            )
        )

    def watch(
        self,
        group: str,
        version: str,
        resource: str,
        namespace: str | None,
        heartbeat_seconds: int | None = None,
        allow_bookmarks: bool = False,
        since_rv: int | None = None,
    ) -> Iterator[tuple[str, K8sObject]]:
        last_rv = int(since_rv or 0)
        known: dict[tuple[str | None, str], K8sObject] = {}
        last_heartbeat = time.time()

        def _snapshot() -> list[K8sObject]:
            if namespace is None:
                return self.list_all(group, version, resource)
            return self.list(group, version, resource, namespace)

        initial = _snapshot()
        if since_rv <= 0:
            for obj in initial:
                key = (obj.namespace, obj.name)
                known[key] = obj
                last_rv = max(last_rv, int(obj.resource_version))
                yield ("ADDED", obj)
        else:
            for obj in initial:
                key = (obj.namespace, obj.name)
                known[key] = obj
                if int(obj.resource_version) > last_rv:
                    last_rv = max(last_rv, int(obj.resource_version))
                    yield ("ADDED", obj)
            last_heartbeat = time.time()

        while True:
            time.sleep(self._watch_poll_interval)
            current = _snapshot()
            current_map = {(obj.namespace, obj.name): obj for obj in current}
            events: list[tuple[str, K8sObject]] = []
            for key, obj in current_map.items():
                prev = known.get(key)
                if prev is None:
                    events.append(("ADDED", obj))
                    continue
                if int(obj.resource_version) > int(prev.resource_version):
                    events.append(("MODIFIED", obj))
            for key, prev in known.items():
                if key not in current_map:
                    events.append(("DELETED", prev))
            events.sort(key=lambda item: (int(item[1].resource_version), item[1].name))
            if events:
                for event_type, obj in events:
                    known[(obj.namespace, obj.name)] = obj
                    if event_type == "DELETED":
                        known.pop((obj.namespace, obj.name), None)
                    last_rv = max(last_rv, int(obj.resource_version))
                    yield (event_type, obj)
                last_heartbeat = time.time()
                continue
            if allow_bookmarks and heartbeat_seconds and (time.time() - last_heartbeat) >= heartbeat_seconds:
                yield (
                    "BOOKMARK",
                    K8sObject(
                        group,
                        version,
                        resource,
                        namespace,
                        "",
                        {},
                        {},
                        {"resourceVersion": last_rv},
                        last_rv,
                    ),
                )
                last_heartbeat = time.time()


class GenericAuthorityStore:
    """Store adapter exposing non-workload HA resources via controller state."""

    def __init__(self, state: SQLiteStateStore) -> None:
        self._state = state
        self.backend = "ha-controller-generic"
        self._watch_poll_interval = max(
            0.1, float(os.getenv("AE_APISHIM_HA_WATCH_POLL_SEC", "0.5") or "0.5")
        )

    def close(self) -> None:
        return None

    def export_all(self):
        return iter(())

    def render_metrics(self) -> str:
        return ""

    def get(
        self, group: str, version: str, resource: str, namespace: str | None, name: str
    ) -> K8sObject | None:
        if not is_generic_authority_resource(group, version, resource):
            return None
        entry = self._state.get_authority_object(group, version, resource, namespace, name)
        return _authority_object_to_k8s(entry) if entry is not None else None

    def list(
        self, group: str, version: str, resource: str, namespace: str | None | None
    ) -> list[K8sObject]:
        if not is_generic_authority_resource(group, version, resource):
            return []
        entries = self._state.list_authority_objects(group, version, resource, namespace)
        return [_authority_object_to_k8s(entry) for entry in entries]

    def list_all(self, group: str, version: str, resource: str) -> list[K8sObject]:
        return self.list(group, version, resource, None)

    def upsert(
        self,
        group: str,
        version: str,
        resource: str,
        namespace: str | None,
        name: str,
        metadata: dict[str, Any],
        spec: dict[str, Any],
        status: dict[str, Any] | None = None,
        resource_version: int | None = None,
    ) -> K8sObject:
        if not is_generic_authority_resource(group, version, resource):
            raise AuthorityMutationError(message=f"unsupported authority resource {group}/{version}/{resource}")
        if (group, version, resource) == ("autoscaling", "v2", "horizontalpodautoscalers"):
            _validate_hpa_spec(spec)
        existing = self._state.get_authority_object(group, version, resource, namespace, name)
        expected_rv = _rv_from_metadata(metadata)
        if expected_rv is None:
            expected_rv = existing.resource_version if existing is not None else 0
        kind = str((metadata or {}).get("kind") or (existing.kind if existing is not None else generic_kind_for_resource(resource)))
        self._state.register_authority_object(
            group,
            version,
            resource,
            namespace,
            name,
            kind=kind,
            metadata=metadata,
            spec=spec,
            status=status or {},
            expected_resource_version=expected_rv,
        )
        fresh = self._state.get_authority_object(group, version, resource, namespace, name)
        if fresh is None:
            raise AuthorityMutationError(
                status_code=int(HTTPStatus.INTERNAL_SERVER_ERROR),
                reason="InternalError",
                message="failed to read back shared authority object",
            )
        return _authority_object_to_k8s(fresh)

    def upsert_if_not_deleted(
        self,
        group: str,
        version: str,
        resource: str,
        namespace: str | None,
        name: str,
        metadata: dict[str, Any],
        spec: dict[str, Any],
        status: dict[str, Any] | None = None,
        resource_version: int | None = None,
    ) -> K8sObject | None:
        return self.upsert(
            group,
            version,
            resource,
            namespace,
            name,
            metadata,
            spec,
            status=status,
            resource_version=resource_version,
        )

    def delete(
        self, group: str, version: str, resource: str, namespace: str | None, name: str
    ) -> bool:
        if not is_generic_authority_resource(group, version, resource):
            return False
        entry = self._state.get_authority_object(group, version, resource, namespace, name)
        if entry is None:
            return False
        return bool(
            self._state.delete_authority_object(
                group,
                version,
                resource,
                namespace,
                name,
                expected_resource_version=entry.resource_version,
            )
        )

    def watch(
        self,
        group: str,
        version: str,
        resource: str,
        namespace: str | None,
        heartbeat_seconds: int | None = None,
        allow_bookmarks: bool = False,
        since_rv: int | None = None,
    ) -> Iterator[tuple[str, K8sObject]]:
        last_rv = int(since_rv or 0)
        known: dict[tuple[str | None, str], K8sObject] = {}
        last_heartbeat = time.time()

        def _snapshot() -> list[K8sObject]:
            if namespace is None:
                return self.list_all(group, version, resource)
            return self.list(group, version, resource, namespace)

        initial = _snapshot()
        if since_rv <= 0:
            for obj in initial:
                key = (obj.namespace, obj.name)
                known[key] = obj
                last_rv = max(last_rv, int(obj.resource_version))
                yield ("ADDED", obj)
        else:
            for obj in initial:
                key = (obj.namespace, obj.name)
                known[key] = obj
                if int(obj.resource_version) > last_rv:
                    last_rv = max(last_rv, int(obj.resource_version))
                    yield ("ADDED", obj)
            last_heartbeat = time.time()

        while True:
            time.sleep(self._watch_poll_interval)
            current = _snapshot()
            current_map = {(obj.namespace, obj.name): obj for obj in current}
            events: list[tuple[str, K8sObject]] = []
            for key, obj in current_map.items():
                prev = known.get(key)
                if prev is None:
                    events.append(("ADDED", obj))
                    continue
                if int(obj.resource_version) > int(prev.resource_version):
                    events.append(("MODIFIED", obj))
            for key, prev in known.items():
                if key not in current_map:
                    events.append(("DELETED", prev))
            events.sort(key=lambda item: (int(item[1].resource_version), item[1].name))
            if events:
                for event_type, obj in events:
                    known[(obj.namespace, obj.name)] = obj
                    if event_type == "DELETED":
                        known.pop((obj.namespace, obj.name), None)
                    last_rv = max(last_rv, int(obj.resource_version))
                    yield (event_type, obj)
                last_heartbeat = time.time()
                continue
            if allow_bookmarks and heartbeat_seconds and (time.time() - last_heartbeat) >= heartbeat_seconds:
                yield (
                    "BOOKMARK",
                    K8sObject(
                        group,
                        version,
                        resource,
                        namespace,
                        "",
                        {},
                        {},
                        {"resourceVersion": last_rv},
                        last_rv,
                    ),
                )
                last_heartbeat = time.time()


class MultiplexApishimStore:
    """Route converged HA workload resources to controller state and everything else to legacy store."""

    def __init__(
        self,
        authority: WorkloadAuthorityStore,
        generic_authority: GenericAuthorityStore,
        legacy: ObjectStore,
    ) -> None:
        self._authority = authority
        self._generic_authority = generic_authority
        self._legacy = legacy
        self.backend = "mux"

    @classmethod
    def from_state_and_legacy(
        cls, state: SQLiteStateStore, legacy: ObjectStore
    ) -> "MultiplexApishimStore":
        return cls(WorkloadAuthorityStore(state), GenericAuthorityStore(state), legacy)

    def close(self) -> None:
        self._authority.close()
        self._generic_authority.close()
        self._legacy.close()

    def export_all(self):
        return self._legacy.export_all()

    def render_metrics(self) -> str:
        parts = [
            self._authority.render_metrics().rstrip(),
            self._generic_authority.render_metrics().rstrip(),
            self._legacy.render_metrics().rstrip(),
        ]
        return "\n".join(part for part in parts if part) + "\n"

    def _delegate(self, group: str, version: str, resource: str):
        if is_workload_authority_resource(group, version, resource):
            return self._authority
        if is_generic_authority_resource(group, version, resource):
            return self._generic_authority
        return self._legacy

    def get(self, group: str, version: str, resource: str, namespace: str | None, name: str):
        return self._delegate(group, version, resource).get(group, version, resource, namespace, name)

    def list(self, group: str, version: str, resource: str, namespace: str | None | None):
        return self._delegate(group, version, resource).list(group, version, resource, namespace)

    def list_all(self, group: str, version: str, resource: str):
        return self._delegate(group, version, resource).list_all(group, version, resource)

    def watch(
        self,
        group: str,
        version: str,
        resource: str,
        namespace: str | None,
        heartbeat_seconds: int | None = None,
        allow_bookmarks: bool = False,
        since_rv: int | None = None,
    ):
        return self._delegate(group, version, resource).watch(
            group,
            version,
            resource,
            namespace,
            heartbeat_seconds=heartbeat_seconds,
            allow_bookmarks=allow_bookmarks,
            since_rv=since_rv,
        )

    def upsert(
        self,
        group: str,
        version: str,
        resource: str,
        namespace: str | None,
        name: str,
        metadata: dict[str, Any],
        spec: dict[str, Any],
        status: dict[str, Any] | None = None,
        resource_version: int | None = None,
    ):
        return self._delegate(group, version, resource).upsert(
            group,
            version,
            resource,
            namespace,
            name,
            metadata,
            spec,
            status=status,
            resource_version=resource_version,
        )

    def upsert_if_not_deleted(
        self,
        group: str,
        version: str,
        resource: str,
        namespace: str | None,
        name: str,
        metadata: dict[str, Any],
        spec: dict[str, Any],
        status: dict[str, Any] | None = None,
        resource_version: int | None = None,
    ):
        return self._delegate(group, version, resource).upsert_if_not_deleted(
            group,
            version,
            resource,
            namespace,
            name,
            metadata,
            spec,
            status=status,
            resource_version=resource_version,
        )

    def delete(self, group: str, version: str, resource: str, namespace: str | None, name: str):
        return self._delegate(group, version, resource).delete(
            group,
            version,
            resource,
            namespace,
            name,
        )
