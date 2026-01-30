"""Kubernetes exporter: convert AppManifest to upstream K8s YAML.

This module maps our ae.dev/v1alpha1 App spec to a minimal, portable set of
Kubernetes resources:

- Deployment (apps/v1)
- Service (v1) when ports exist
- Ingress (networking.k8s.io/v1) when manifest.spec.ingress exists

It intentionally sticks to stable APIs and a conservative subset aligned with
the guidance captured in FEAT.md Task 1.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import yaml

from ae.controller.spec import DEFAULT_NAMESPACE, AppManifest, k8s_labels_for_manifest


@dataclass(slots=True)
class ExportOptions:
    # Which workload to emit
    workload_kind: str = "Deployment"  # "Deployment" | "StatefulSet" | "Job" | "CronJob"
    namespace: Optional[str] = None  # default: manifest namespace or "default"
    ingress_class_name: Optional[str] = None
    service_port: Optional[int] = None  # override; default to 80
    # Config/Secret emission control
    emit_configs: bool = False
    inline_configs: bool = False
    emit_secrets: bool = False
    inline_secrets: bool = False  # caution: expects plaintext YAML/JSON
    # Storage emission
    emit_storage: bool = False
    default_pvc_size: str = "1Gi"
    storage_class_name: Optional[str] = None
    # Optional accessModes override for PVCs (e.g., ["ReadWriteOnce", "ReadOnlyMany"])
    pvc_access_modes: Optional[List[str]] = None
    # ServiceAccount
    service_account_name: Optional[str] = None
    # Policy / rollouts
    emit_pdb: bool = False
    # HPA
    hpa_min: Optional[int] = None
    hpa_max: Optional[int] = None
    hpa_cpu_target: Optional[int] = None  # averageUtilization percent
    hpa_mem_target: Optional[int] = None  # averageUtilization percent
    hpa_mem_type: Optional[str] = None  # 'utilization' (default) or 'value'
    hpa_mem_value: Optional[str] = None  # e.g., '200Mi' for AverageValue
    # HPA guards
    allow_hpa_without_requests: bool = False
    # HPA behavior (autoscaling/v2): optional scaleUp/scaleDown behavior dicts
    hpa_behavior_up: Optional[Dict[str, Any]] = None
    hpa_behavior_down: Optional[Dict[str, Any]] = None
    # Security defaults
    default_security: bool = False
    # PDB tuning (accepts integers or percentage strings, e.g., "50%")
    pdb_min_available: Optional[str | int] = None
    pdb_max_unavailable: Optional[str | int] = None
    # Strictness
    require_requests: bool = False
    # NetworkPolicy generation
    emit_network_policy: bool = False
    np_default_deny_ingress: bool = False
    np_default_deny_egress: bool = False
    np_allow_dns: bool = False
    np_allow_web: bool = False  # allow TCP 80/443 egress when default-deny egress
    # Allow egress to RFC1918 private CIDRs for specific TCP ports (e.g., DB/cache)
    np_allow_internal_ports: list[int] = field(default_factory=list)
    # Topology spread (inject if none provided)
    inject_topology_spread: bool = False
    # Ingress annotations (passthrough)
    ingress_annotations: Optional[Dict[str, Any]] = None
    # Ingress pathType selection (Prefix, Exact, ImplementationSpecific)
    ingress_path_type: Optional[str] = None
    # Job options
    job_backoff_limit: Optional[int] = None
    job_ttl_seconds_after_finished: Optional[int] = None
    # CronJob options
    cron_schedule: Optional[str] = None
    cron_concurrency_policy: Optional[str] = None  # Allow|Forbid|Replace
    cron_suspend: Optional[bool] = None
    cron_starting_deadline_seconds: Optional[int] = None
    # Namespace emission with PodSecurity labels
    emit_namespace: bool = False
    pod_security_enforce: Optional[str] = None  # baseline|restricted


def _resolve_namespace(m: AppManifest, opts: ExportOptions) -> str:
    if getattr(opts, "namespace", None):
        return str(opts.namespace)
    ns = getattr(m.metadata, "namespace", None)
    return str(ns or DEFAULT_NAMESPACE)


def _resource_labels(m: AppManifest) -> Dict[str, Any]:
    return dict(k8s_labels_for_manifest(m))


def _selector_labels(m: AppManifest) -> Dict[str, Any]:
    labels = _resource_labels(m)
    selector_keys = ("app", "app.kubernetes.io/name", "app.kubernetes.io/instance")
    return {k: labels[k] for k in selector_keys if k in labels}


def _container_from_manifest(m: AppManifest, *, opts: ExportOptions) -> Dict[str, Any]:
    spec = m.spec
    c: Dict[str, Any] = {
        "name": m.metadata.name,
        "image": spec.image,
    }
    if spec.command:
        c["command"] = list(spec.command)
    if getattr(spec, "args", None):
        c["args"] = list(spec.args)
    if getattr(spec, "working_dir", None):
        c["workingDir"] = str(spec.working_dir)
    if getattr(spec, "termination_message_path", None):
        c["terminationMessagePath"] = str(spec.termination_message_path)
    if getattr(spec, "termination_message_policy", None):
        c["terminationMessagePolicy"] = str(spec.termination_message_policy)
    # image pull policy
    if getattr(spec, "image_pull_policy", None):
        c["imagePullPolicy"] = str(spec.image_pull_policy)
    # env: start with explicit pairs
    env: List[Dict[str, Any]] = []
    for item in spec.env:
        name = item.get("name")
        if name is None:
            continue
        entry: Dict[str, Any] = {"name": name}
        if "valueFrom" in item and isinstance(item.get("valueFrom"), dict):
            # Pass through common valueFrom forms (fieldRef, configMapKeyRef, secretKeyRef, resourceFieldRef)
            entry["valueFrom"] = item["valueFrom"]
        else:
            entry["value"] = item.get("value", "")
        env.append(entry)
    # env via configRefs/secretRefs key mappings
    for ref in getattr(spec, "config_refs", []) or []:
        for mapp in ref.env:
            env.append(
                {
                    "name": mapp.name,
                    "valueFrom": {"configMapKeyRef": {"name": ref.name, "key": mapp.key}},
                }
            )
    for ref in getattr(spec, "secret_refs", []) or []:
        for mapp in ref.env:
            env.append(
                {
                    "name": mapp.name,
                    "valueFrom": {"secretKeyRef": {"name": ref.name, "key": mapp.key}},
                }
            )
    # envFrom: opt-in on refs
    for ref in getattr(spec, "config_refs", []) or []:
        if bool(getattr(ref, "env_from", False)):
            env.append({"name": "", "valueFrom": {"configMapKeyRef": {"name": ref.name, "key": ""}}})
    for ref in getattr(spec, "secret_refs", []) or []:
        if bool(getattr(ref, "env_from", False)):
            env.append({"name": "", "valueFrom": {"secretKeyRef": {"name": ref.name, "key": ""}}})
    if env:
        # sanitize envFrom hack entries by moving into envFrom list
        env_from: List[Dict[str, Any]] = []
        real_env: List[Dict[str, Any]] = []
        for e in env:
            if e.get("name") == "" and e.get("valueFrom"):  # our marker
                if "configMapKeyRef" in e["valueFrom"]:
                    cm_ref = e["valueFrom"]["configMapKeyRef"]
                    entry: Dict[str, Any] = {"configMapRef": {"name": cm_ref["name"]}}
                    if cm_ref.get("prefix"):
                        entry["prefix"] = cm_ref.get("prefix")
                    env_from.append(entry)
                elif "secretKeyRef" in e["valueFrom"]:
                    sec_ref = e["valueFrom"]["secretKeyRef"]
                    entry = {"secretRef": {"name": sec_ref["name"]}}
                    if sec_ref.get("prefix"):
                        entry["prefix"] = sec_ref.get("prefix")
                    env_from.append(entry)
            else:
                real_env.append(e)
        if real_env:
            c["env"] = real_env
        if env_from:
            c["envFrom"] = env_from
    # ports
    if spec.ports:
        c["ports"] = [{"name": p.name, "containerPort": int(p.container_port)} for p in spec.ports]
    # resources
    if spec.resources:
        res: Dict[str, Any] = {}
        if spec.resources.requests:
            req: Dict[str, Any] = {}
            if spec.resources.requests.cpu is not None:
                # K8s expects millicores or cores; pass through as string
                req["cpu"] = str(spec.resources.requests.cpu)
            if spec.resources.requests.memory is not None:
                req["memory"] = str(spec.resources.requests.memory)
            if req:
                res["requests"] = req
        if spec.resources.limits:
            lim: Dict[str, Any] = {}
            if spec.resources.limits.cpu is not None:
                lim["cpu"] = str(spec.resources.limits.cpu)
            if spec.resources.limits.memory is not None:
                lim["memory"] = str(spec.resources.limits.memory)
            if lim:
                res["limits"] = lim
        if res:
            c["resources"] = res
    # securityContext
    if spec.security:
        sc: Dict[str, Any] = {}
        if spec.security.run_as_user is not None:
            sc["runAsUser"] = int(spec.security.run_as_user)
        if spec.security.run_as_group is not None:
            sc["runAsGroup"] = int(spec.security.run_as_group)
        if spec.security.read_only_root:
            sc["readOnlyRootFilesystem"] = True
        if spec.security.drop_caps:
            sc["capabilities"] = {"drop": list(spec.security.drop_caps)}
        # seccomp profile
        if getattr(spec.security, "seccomp_type", None):
            seccomp: Dict[str, Any] = {"type": str(spec.security.seccomp_type)}
            if str(spec.security.seccomp_type) == "Localhost" and getattr(
                spec.security, "seccomp_localhost_profile", None
            ):
                seccomp["localhostProfile"] = str(spec.security.seccomp_localhost_profile)
            sc["seccompProfile"] = seccomp
        if sc:
            c["securityContext"] = sc
    elif opts.default_security:
        c["securityContext"] = {
            "runAsNonRoot": True,
            "readOnlyRootFilesystem": True,
            "allowPrivilegeEscalation": False,
            "capabilities": {"drop": ["ALL"]},
            "seccompProfile": {"type": "RuntimeDefault"},
        }
    # probes
    if spec.health:
        if spec.health.readiness:
            c["readinessProbe"] = _probe_to_k8s(spec.health.readiness)
        if spec.health.liveness:
            c["livenessProbe"] = _probe_to_k8s(spec.health.liveness)
        if getattr(spec.health, "startup", None):
            c["startupProbe"] = _probe_to_k8s(spec.health.startup)
    # lifecycle hooks
    if getattr(spec, "lifecycle", None):
        lifecycle: Dict[str, Any] = {}

        def _lh_to_k8s(h) -> Dict[str, Any]:  # noqa: ANN001
            if h is None:
                return {}
            # Accept both LifecycleHandler model and raw dict
            if isinstance(h, dict):
                if h.get("exec") is not None:
                    return {"exec": {"command": list(h.get("exec", {}).get("command", []))}}
                if h.get("httpGet") is not None:
                    hg = h.get("httpGet", {})
                    return {
                        "httpGet": {"path": (hg.get("path") or "/"), "port": int(hg.get("port", 0))}
                    }
                if h.get("tcpSocket") is not None:
                    ts = h.get("tcpSocket", {})
                    return {"tcpSocket": {"port": int(ts.get("port", 0))}}
                return {}
            if getattr(h, "exec", None) is not None:
                return {"exec": {"command": list(h.exec.command)}}
            if getattr(h, "http_get", None) is not None:
                return {"httpGet": {"path": h.http_get.path or "/", "port": int(h.http_get.port)}}
            if getattr(h, "tcp_socket", None) is not None:
                return {"tcpSocket": {"port": int(h.tcp_socket.port)}}
            return {}

        lc = getattr(spec, "lifecycle")
        post = getattr(lc, "post_start", None) if lc is not None else None
        pre = getattr(lc, "pre_stop", None) if lc is not None else None
        # Fallback to dict keys when using raw dict updates in tests/tools
        if post is None and isinstance(lc, dict):
            post = lc.get("postStart") or lc.get("post_start")
        if pre is None and isinstance(lc, dict):
            pre = lc.get("preStop") or lc.get("pre_stop")
        if post is not None:
            lifecycle["postStart"] = _lh_to_k8s(post)
        if pre is not None:
            lifecycle["preStop"] = _lh_to_k8s(pre)
        if lifecycle:
            c["lifecycle"] = lifecycle
    return c


def _container_from_spec(
    m: AppManifest,
    csp,
    *,
    opts: ExportOptions,
    allow_probes: bool = True,
    projected_volume_name: str | None = None,
) -> Dict[str, Any]:
    """Build a K8s container dict from a ContainerSpec-like object."""

    def _gf(obj, field, default=None):
        if isinstance(obj, dict):
            return obj.get(field, default)
        return getattr(obj, field, default)

    c: Dict[str, Any] = {"name": str(_gf(csp, "name")), "image": str(_gf(csp, "image"))}
    if _gf(csp, "command"):
        c["command"] = list(_gf(csp, "command"))
    if _gf(csp, "args"):
        c["args"] = list(_gf(csp, "args"))
    image_pull_policy = _gf(csp, "image_pull_policy")
    if image_pull_policy is None and isinstance(csp, dict):
        image_pull_policy = csp.get("imagePullPolicy")
    if image_pull_policy:
        c["imagePullPolicy"] = str(image_pull_policy)
    # env
    env: List[Dict[str, Any]] = []
    for item in _gf(csp, "env") or []:
        name = item.get("name")
        if name is None:
            continue
        entry: Dict[str, Any] = {"name": name}
        if "valueFrom" in item and isinstance(item.get("valueFrom"), dict):
            entry["valueFrom"] = item["valueFrom"]
        else:
            entry["value"] = item.get("value", "")
        env.append(entry)
    # global envFrom via refs
    for ref in getattr(m.spec, "config_refs", []) or []:
        if bool(getattr(ref, "env_from", False)):
            env.append(
                {"name": "", "valueFrom": {"configMapKeyRef": {"name": ref.name, "key": ""}}}
            )
    for ref in getattr(m.spec, "secret_refs", []) or []:
        if bool(getattr(ref, "env_from", False)):
            env.append({"name": "", "valueFrom": {"secretKeyRef": {"name": ref.name, "key": ""}}})
    if env:
        # sanitize envFrom hack entries by moving into envFrom list
        env_from: List[Dict[str, Any]] = []
        real_env: List[Dict[str, Any]] = []
        for e in env:
            if e.get("name") == "" and e.get("valueFrom"):  # our marker
                if "configMapKeyRef" in e["valueFrom"]:
                    cm_ref = e["valueFrom"]["configMapKeyRef"]
                    entry: Dict[str, Any] = {"configMapRef": {"name": cm_ref["name"]}}
                    if cm_ref.get("prefix"):
                        entry["prefix"] = cm_ref.get("prefix")
                    env_from.append(entry)
                elif "secretKeyRef" in e["valueFrom"]:
                    sec_ref = e["valueFrom"]["secretKeyRef"]
                    entry = {"secretRef": {"name": sec_ref["name"]}}
                    if sec_ref.get("prefix"):
                        entry["prefix"] = sec_ref.get("prefix")
                    env_from.append(entry)
            else:
                real_env.append(e)
        if real_env:
            c["env"] = real_env
        if env_from:
            c["envFrom"] = env_from
    # ports
    if _gf(csp, "ports"):
        c["ports"] = [
            {"name": p.name, "containerPort": int(p.container_port)} for p in _gf(csp, "ports")
        ]
    # resources
    if _gf(csp, "resources"):
        rs = _gf(csp, "resources")
        res: Dict[str, Any] = {}
        if rs.requests:
            req = {}
            if rs.requests.cpu is not None:
                req["cpu"] = str(rs.requests.cpu)
            if rs.requests.memory is not None:
                req["memory"] = str(rs.requests.memory)
            if req:
                res["requests"] = req
        if rs.limits:
            lim = {}
            if rs.limits.cpu is not None:
                lim["cpu"] = str(rs.limits.cpu)
            if rs.limits.memory is not None:
                lim["memory"] = str(rs.limits.memory)
            if lim:
                res["limits"] = lim
        if res:
            c["resources"] = res
    # security
    if getattr(csp, "security", None):
        sc: Dict[str, Any] = {}
        sec = csp.security if not isinstance(csp, dict) else None
        if sec is None and isinstance(csp, dict) and csp.get("security"):
            sec = csp["security"]
        if sec.run_as_user is not None:
            sc["runAsUser"] = int(sec.run_as_user)
        if sec.run_as_group is not None:
            sc["runAsGroup"] = int(sec.run_as_group)
        if sec.read_only_root:
            sc["readOnlyRootFilesystem"] = True
        if sec.drop_caps:
            sc["capabilities"] = {"drop": list(sec.drop_caps)}
        if getattr(sec, "seccomp_type", None):
            s = {"type": str(sec.seccomp_type)}
            if str(sec.seccomp_type) == "Localhost" and getattr(
                sec, "seccomp_localhost_profile", None
            ):
                s["localhostProfile"] = str(sec.seccomp_localhost_profile)
            sc["seccompProfile"] = s
        if sc:
            c["securityContext"] = sc
    # working dir
    if _gf(csp, "working_dir"):
        c["workingDir"] = str(_gf(csp, "working_dir"))
    # probes
    if allow_probes and _gf(csp, "health"):
        h = _gf(csp, "health")
        if h.readiness:
            c["readinessProbe"] = _probe_to_k8s(h.readiness)
        if h.liveness:
            c["livenessProbe"] = _probe_to_k8s(h.liveness)
        if getattr(h, "startup", None):
            c["startupProbe"] = _probe_to_k8s(h.startup)
    # lifecycle (per-container)
    if getattr(csp, "lifecycle", None):
        lc_dict: Dict[str, Any] = {}
        lc = getattr(csp, "lifecycle")

        def _lh_to_k8s(h) -> Dict[str, Any]:  # noqa: ANN001
            if h is None:
                return {}
            if isinstance(h, dict):
                if h.get("exec") is not None:
                    return {"exec": {"command": list(h.get("exec", {}).get("command", []))}}
                if h.get("httpGet") is not None:
                    hg = h.get("httpGet", {})
                    return {
                        "httpGet": {"path": (hg.get("path") or "/"), "port": int(hg.get("port", 0))}
                    }
                if h.get("tcpSocket") is not None:
                    ts = h.get("tcpSocket", {})
                    return {"tcpSocket": {"port": int(ts.get("port", 0))}}
                return {}
            if getattr(h, "exec", None) is not None:
                return {"exec": {"command": list(h.exec.command)}}
            if getattr(h, "http_get", None) is not None:
                return {"httpGet": {"path": h.http_get.path or "/", "port": int(h.http_get.port)}}
            if getattr(h, "tcp_socket", None) is not None:
                return {"tcpSocket": {"port": int(h.tcp_socket.port)}}
            return {}

        post = getattr(lc, "post_start", None) if lc is not None else None
        pre = getattr(lc, "pre_stop", None) if lc is not None else None
        if post is None and isinstance(lc, dict):
            post = lc.get("postStart") or lc.get("post_start")
        if pre is None and isinstance(lc, dict):
            pre = lc.get("preStop") or lc.get("pre_stop")
        if post is not None:
            lc_dict["postStart"] = _lh_to_k8s(post)
        if pre is not None:
            lc_dict["preStop"] = _lh_to_k8s(pre)
        if lc_dict:
            c["lifecycle"] = lc_dict

    # per-container projectionMounts via subPath on the projected volume
    if projected_volume_name and getattr(csp, "projection_mounts", None):
        vms = c.setdefault("volumeMounts", [])
        for pm in getattr(csp, "projection_mounts", []) or []:
            path = getattr(pm, "path", None) if not isinstance(pm, dict) else pm.get("path")
            mnt = (
                getattr(pm, "mount_path", None)
                if not isinstance(pm, dict)
                else pm.get("mountPath") or pm.get("mount_path")
            )
            ro = (
                bool(getattr(pm, "read_only", True))
                if not isinstance(pm, dict)
                else bool(pm.get("readOnly", True))
            )
            if path and mnt:
                vms.append(
                    {
                        "name": projected_volume_name,
                        "mountPath": str(mnt),
                        "subPath": str(path),
                        "readOnly": ro,
                    }
                )
    return c


def _probe_to_k8s(probe) -> Dict[str, Any]:  # ProbeSpec
    out: Dict[str, Any] = {
        "initialDelaySeconds": int(probe.initial_delay_seconds),
        "timeoutSeconds": int(probe.timeout_seconds),
        "periodSeconds": int(probe.period_seconds),
        "successThreshold": int(probe.success_threshold),
        "failureThreshold": int(probe.failure_threshold),
    }
    if probe.http_get is not None:
        out["httpGet"] = {"path": probe.http_get.path or "/", "port": int(probe.http_get.port)}
    elif probe.tcp_socket is not None:
        out["tcpSocket"] = {"port": int(probe.tcp_socket.port)}
    elif probe.exec is not None:
        out["exec"] = {"command": list(probe.exec.command)}
    return out


def _service_from_manifest(m: AppManifest, opts: ExportOptions) -> Optional[Dict[str, Any]]:
    spec = m.spec
    if not spec.ports:
        return None
    # If explicit multi-port mapping is provided, honor it
    svc_ports: List[Dict[str, Any]] = []
    if getattr(spec, "service", None) and getattr(spec.service, "ports", None):
        # Build a quick index of container ports by name/number
        by_name = {p.name: int(p.container_port) for p in spec.ports if getattr(p, "name", None)}
        by_num = {int(p.container_port): int(p.container_port) for p in spec.ports}
        # NodePort validation window (K8s default range)
        NP_MIN, NP_MAX = 30000, 32767
        # Duplicate detection
        seen_names: set[str] = set()
        seen_ports: set[int] = set()
        seen_nodeports: set[int] = set()
        for sp in spec.service.ports:
            # names must be unique
            if sp.name in seen_names:
                raise ValueError(f"duplicate Service port name: {sp.name}")
            seen_names.add(sp.name)
            # port numbers must be unique (per protocol)
            if int(sp.port) in seen_ports:
                raise ValueError(f"duplicate Service port: {int(sp.port)}")
            seen_ports.add(int(sp.port))
            tgt = sp.target_port
            if tgt is None:
                # Prefer number equal to named container port if name matches; else same number
                tgt = (
                    by_name.get(sp.name) or by_num.get(int(sp.port)) or next(iter(by_num.values()))
                )
            entry = {
                "name": sp.name,
                "port": int(sp.port),
                "targetPort": int(tgt),
                "protocol": getattr(sp, "protocol", "TCP") or "TCP",
            }
            # Only include nodePort when Service.type is NodePort/LoadBalancer and node_port is provided
            if (
                getattr(spec.service, "type", None) in {"NodePort", "LoadBalancer"}
                and getattr(sp, "node_port", None) is not None
            ):
                # Validate nodePort range
                np = int(sp.node_port)
                if not (NP_MIN <= np <= NP_MAX):
                    raise ValueError(f"nodePort {np} out of allowed range [{NP_MIN}-{NP_MAX}]")
                if np in seen_nodeports:
                    raise ValueError(f"duplicate Service nodePort: {np}")
                seen_nodeports.add(np)
                entry["nodePort"] = int(sp.node_port)
            svc_ports.append(entry)
    else:
        # Back-compat single-port Service: map one HTTP port to the first container port
        first = spec.ports[0].container_port if spec.ports else 8080
        target = (
            int(spec.service.target_port)
            if (spec.service and spec.service.target_port)
            else int(first)
        )
        port = int(
            opts.service_port
            or (spec.service.port if spec.service and spec.service.port is not None else 80)
        )
        svc_ports.append({"name": "http", "port": port, "targetPort": target})

    labels = _resource_labels(m)
    selector = _selector_labels(m)
    body = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {
            "name": m.metadata.name,
            "namespace": _resolve_namespace(m, opts),
            "labels": labels,
        },
        "spec": {
            "selector": selector,
            "ports": svc_ports,
        },
    }
    # Optional service fields
    if getattr(spec, "service", None):
        if getattr(spec.service, "type", None):
            body["spec"]["type"] = spec.service.type
            if getattr(spec.service, "external_traffic_policy", None) is not None:
                body["spec"]["externalTrafficPolicy"] = spec.service.external_traffic_policy
        if getattr(spec.service, "external_ips", None):
            body["spec"]["externalIPs"] = list(spec.service.external_ips)
        # Session affinity pass-through
        if getattr(spec.service, "session_affinity", None) is not None:
            body["spec"]["sessionAffinity"] = spec.service.session_affinity
        cfg = getattr(spec.service, "session_affinity_config", None)
        if cfg and getattr(cfg, "client_ip", None) is not None:
            to = getattr(cfg.client_ip, "timeout_seconds", None)
            if to is not None:
                body["spec"]["sessionAffinityConfig"] = {"clientIP": {"timeoutSeconds": int(to)}}
    return body


def _deployment_from_manifest(m: AppManifest, opts: ExportOptions) -> Dict[str, Any]:
    # Containers: use multi-container list when present; else build from top-level spec
    containers: List[Dict[str, Any]] = []
    if getattr(m.spec, "containers", None):
        for csp in m.spec.containers:
            containers.append(_container_from_spec(m, csp, opts=opts, allow_probes=True))
    else:
        containers.append(_container_from_manifest(m, opts=opts))
    pod_spec: Dict[str, Any] = {"containers": containers}
    if m.spec.termination_grace_period_seconds is not None:
        pod_spec["terminationGracePeriodSeconds"] = int(m.spec.termination_grace_period_seconds)
    if getattr(m.spec, "priority_class_name", None):
        pod_spec["priorityClassName"] = str(m.spec.priority_class_name)
    if getattr(m.spec, "hostname", None):
        pod_spec["hostname"] = str(m.spec.hostname)
    if getattr(m.spec, "subdomain", None):
        pod_spec["subdomain"] = str(m.spec.subdomain)
    if getattr(m.spec, "host_aliases", None):
        aliases = []
        for ha in m.spec.host_aliases:
            if isinstance(ha, dict):
                ip = ha.get("ip")
                hns = ha.get("hostnames") or []
            else:
                ip = getattr(ha, "ip", None)
                hns = list(getattr(ha, "hostnames", []) or [])
            if ip:
                aliases.append({"ip": str(ip), "hostnames": list(hns)})
        if aliases:
            pod_spec["hostAliases"] = aliases
    if getattr(m.spec, "enable_service_links", None) is not None:
        pod_spec["enableServiceLinks"] = bool(m.spec.enable_service_links)
    if getattr(m.spec, "share_process_namespace", None) is not None:
        pod_spec["shareProcessNamespace"] = bool(m.spec.share_process_namespace)
    if getattr(m.spec, "host_network", None) is not None:
        pod_spec["hostNetwork"] = bool(m.spec.host_network)
    if getattr(m.spec, "node_selector", None):
        pod_spec["nodeSelector"] = dict(m.spec.node_selector)
    if getattr(m.spec, "set_hostname_as_fqdn", None) is not None:
        pod_spec["setHostnameAsFQDN"] = bool(m.spec.set_hostname_as_fqdn)
    if getattr(m.spec, "host_pid", None) is not None:
        pod_spec["hostPID"] = bool(m.spec.host_pid)
    if getattr(m.spec, "host_ipc", None) is not None:
        pod_spec["hostIPC"] = bool(m.spec.host_ipc)
    # ServiceAccount
    if opts.service_account_name:
        pod_spec["serviceAccountName"] = opts.service_account_name
    # ImagePullSecrets
    if getattr(m.spec, "image_pull_secrets", None):
        pod_spec["imagePullSecrets"] = [{"name": s} for s in m.spec.image_pull_secrets]
    # DNS policy/config
    if getattr(m.spec, "dns_policy", None):
        pod_spec["dnsPolicy"] = str(m.spec.dns_policy)
    if getattr(m.spec, "dns_config", None):
        dnsc: Dict[str, Any] = {}
        dc = m.spec.dns_config
        if isinstance(dc, dict):
            if dc.get("nameservers"):
                dnsc["nameservers"] = list(dc.get("nameservers", []))
            if dc.get("searches"):
                dnsc["searches"] = list(dc.get("searches", []))
            opt_list = []
            for o in dc.get("options", []) or []:
                name = o.get("name")
                if not name:
                    continue
                ent = {"name": name}
                if o.get("value") is not None:
                    ent["value"] = str(o.get("value"))
                opt_list.append(ent)
            if opt_list:
                dnsc["options"] = opt_list
        else:
            if getattr(dc, "nameservers", None):
                dnsc["nameservers"] = list(dc.nameservers)
            if getattr(dc, "searches", None):
                dnsc["searches"] = list(dc.searches)
            opt_list = []
            for o in getattr(dc, "options", []) or []:
                ent = {"name": o.name}
                if getattr(o, "value", None) is not None:
                    ent["value"] = str(o.value)
                opt_list.append(ent)
            if opt_list:
                dnsc["options"] = opt_list
        if dnsc:
            pod_spec["dnsConfig"] = dnsc
    # Pod-level securityContext
    if getattr(m.spec, "pod_security", None):
        psc = {}
        if getattr(m.spec.pod_security, "fs_group", None) is not None:
            psc["fsGroup"] = int(m.spec.pod_security.fs_group)
        if getattr(m.spec.pod_security, "seccomp_type", None):
            sec = {"type": str(m.spec.pod_security.seccomp_type)}
            if str(m.spec.pod_security.seccomp_type) == "Localhost" and getattr(
                m.spec.pod_security, "seccomp_localhost_profile", None
            ):
                sec["localhostProfile"] = str(m.spec.pod_security.seccomp_localhost_profile)
            psc["seccompProfile"] = sec
        selinux = {}
        if getattr(m.spec.pod_security, "selinux_user", None):
            selinux["user"] = str(m.spec.pod_security.selinux_user)
        if getattr(m.spec.pod_security, "selinux_role", None):
            selinux["role"] = str(m.spec.pod_security.selinux_role)
        if getattr(m.spec.pod_security, "selinux_type", None):
            selinux["type"] = str(m.spec.pod_security.selinux_type)
        if getattr(m.spec.pod_security, "selinux_level", None):
            selinux["level"] = str(m.spec.pod_security.selinux_level)
        if selinux:
            psc["seLinuxOptions"] = selinux
        if psc:
            pod_spec["securityContext"] = psc
    # Scheduling pass-through
    if getattr(m.spec, "affinity", None):
        pod_spec["affinity"] = dict(m.spec.affinity)
    if getattr(m.spec, "tolerations", None):
        pod_spec["tolerations"] = list(m.spec.tolerations)
    if getattr(m.spec, "topology_spread_constraints", None):
        pod_spec["topologySpreadConstraints"] = list(m.spec.topology_spread_constraints)
    elif opts.inject_topology_spread and int(getattr(m.spec, "replicas", 1) or 1) > 1:
        pod_spec["topologySpreadConstraints"] = [
            {
                "maxSkew": 1,
                "topologyKey": "kubernetes.io/hostname",
                "whenUnsatisfiable": "ScheduleAnyway",
                "labelSelector": {"matchLabels": _selector_labels(m)},
            }
        ]
    # Storage mounts
    volume_specs: List[Dict[str, Any]] = []
    volume_mounts: List[Dict[str, Any]] = []
    # Projected volume from config/secret file projections
    proj = _projected_volume_from_refs(m)
    if proj is not None:
        volume_specs.append(proj)
        volume_mounts.append({"name": proj["name"], "mountPath": "/var/run/ae/config"})
    # Add explicit per-ref volumes when items[] are present (additive for back-compat)
    explicit = _explicit_volumes_from_refs(m)
    if explicit:
        volume_specs.extend(explicit)
    if opts.emit_storage and getattr(m.spec, "storage", None):
        for s in m.spec.storage:
            s_name = getattr(s, "name", None) if not isinstance(s, dict) else s.get("name")
            s_mount = (
                getattr(s, "mount_path", None)
                if not isinstance(s, dict)
                else (s.get("mountPath") or s.get("mount_path"))
            )
            if not s_name or not s_mount:
                continue
            claim = _storage_claim_name(m.metadata.name, s_name)
            volume_specs.append({"name": claim, "persistentVolumeClaim": {"claimName": claim}})
            vm = {"name": claim, "mountPath": s_mount}
            if _storage_read_only(s):
                vm["readOnly"] = True
            volume_mounts.append(vm)
    # emptyDir ephemeral volumes
    if getattr(m.spec, "empty_dirs", None):
        for ed in m.spec.empty_dirs:
            ed_name = getattr(ed, "name", None) if not isinstance(ed, dict) else ed.get("name")
            ed_mount = (
                getattr(ed, "mount_path", None)
                if not isinstance(ed, dict)
                else (ed.get("mountPath") or ed.get("mount_path"))
            )
            if not ed_name or not ed_mount:
                continue
            body: Dict[str, Any] = {}
            medium = getattr(ed, "medium", None) if not isinstance(ed, dict) else ed.get("medium")
            if medium is not None and str(medium) != "":
                body["medium"] = str(medium)
            size_limit = (
                getattr(ed, "size_limit", None)
                if not isinstance(ed, dict)
                else ed.get("sizeLimit") or ed.get("size_limit")
            )
            if size_limit is not None:
                body["sizeLimit"] = str(size_limit)
            volume_specs.append({"name": ed_name, "emptyDir": body or {}})
            volume_mounts.append({"name": ed_name, "mountPath": ed_mount})
    if volume_specs:
        pod_spec["volumes"] = volume_specs
    if volume_mounts:
        for c in pod_spec.get("containers", []) or []:
            c.setdefault("volumeMounts", []).extend(volume_mounts)
    # Add AppArmor annotation on the Pod template when requested
    labels = _resource_labels(m)
    selector = _selector_labels(m)
    pod_meta: Dict[str, Any] = {"labels": dict(labels)}
    if getattr(m.spec, "security", None) and getattr(m.spec.security, "apparmor_profile", None):
        ann_key = f"container.apparmor.security.beta.kubernetes.io/{m.metadata.name}"
        pod_meta["annotations"] = {ann_key: str(m.spec.security.apparmor_profile)}

    pod = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": m.metadata.name,
            "namespace": _resolve_namespace(m, opts),
            "labels": labels,
        },
        "spec": {
            "replicas": int(m.spec.replicas),
            "selector": {"matchLabels": selector},
            "template": {
                "metadata": pod_meta,
                "spec": pod_spec,
            },
        },
    }
    if getattr(m.spec, "init_containers", None):
        proj = _projected_volume_from_refs(m)
        proj_name = proj["name"] if proj is not None else None
        pod["spec"]["template"]["spec"]["initContainers"] = [
            _container_from_spec(
                m, csp, opts=opts, allow_probes=False, projected_volume_name=proj_name
            )
            for csp in m.spec.init_containers
        ]
    return pod
    if getattr(m.spec, "init_containers", None):
        proj_name = proj["name"] if proj is not None else None
        pod["spec"]["template"]["spec"]["initContainers"] = [
            _container_from_spec(
                m, csp, opts=opts, allow_probes=False, projected_volume_name=proj_name
            )
            for csp in m.spec.init_containers
        ]
    return pod


def _storage_claim_name(app: str, name: str) -> str:
    return f"{app}-{name}"


def _storage_field(s, attr: str, *aliases: str) -> Any:
    if isinstance(s, dict):
        for key in (attr, *aliases):
            if key in s:
                return s.get(key)
        return None
    return getattr(s, attr, None)


def _coerce_str_list(value: Any) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, (list, tuple, set)):
        return [str(v) for v in value if v is not None]
    if isinstance(value, str):
        return [value]
    return None


def _storage_access_modes(s, opts: ExportOptions) -> list[str]:
    if opts.pvc_access_modes is not None:
        return list(opts.pvc_access_modes)
    modes = _coerce_str_list(_storage_field(s, "access_modes", "accessModes", "access_modes"))
    return modes or ["ReadWriteOnce"]


def _storage_class_name(s, opts: ExportOptions) -> str | None:
    if opts.storage_class_name is not None:
        return str(opts.storage_class_name)
    raw = _storage_field(
        s, "storage_class", "class", "storageClassName", "storage_class_name"
    )
    if raw in (None, ""):
        return None
    return str(raw)


def _storage_volume_mode(s) -> str | None:
    raw = _storage_field(s, "volume_mode", "volumeMode", "volume_mode")
    if raw in (None, ""):
        return None
    return str(raw)


def _storage_read_only(s) -> bool:
    raw = _storage_field(s, "read_only", "readOnly", "read_only")
    if raw is None:
        return False
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    return bool(raw)


def _headless_service_for_statefulset(
    m: AppManifest, opts: ExportOptions
) -> Optional[Dict[str, Any]]:
    """Emit a headless Service to back a StatefulSet's stable identities."""
    name = f"{m.metadata.name}-headless"
    ports: List[Dict[str, Any]] = []
    if getattr(m.spec, "ports", None):
        ports = [
            {
                "name": p.name,
                "port": int(getattr(p, "container_port", 0) or 80),
                "targetPort": int(getattr(p, "container_port", 0) or 80),
            }
            for p in m.spec.ports
        ]
    labels = _resource_labels(m)
    selector = _selector_labels(m)
    spec: Dict[str, Any] = {"clusterIP": "None", "selector": selector}
    if ports:
        spec["ports"] = ports
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": name, "namespace": _resolve_namespace(m, opts), "labels": labels},
        "spec": spec,
    }


def _statefulset_from_manifest(m: AppManifest, opts: ExportOptions) -> Dict[str, Any]:
    # Containers
    containers: List[Dict[str, Any]] = []
    if getattr(m.spec, "containers", None):
        for csp in m.spec.containers:
            containers.append(_container_from_spec(m, csp, opts=opts, allow_probes=True))
    else:
        containers.append(_container_from_manifest(m, opts=opts))
    pod_spec: Dict[str, Any] = {"containers": containers}
    if m.spec.termination_grace_period_seconds is not None:
        pod_spec["terminationGracePeriodSeconds"] = int(m.spec.termination_grace_period_seconds)
    if getattr(m.spec, "priority_class_name", None):
        pod_spec["priorityClassName"] = str(m.spec.priority_class_name)
    if getattr(m.spec, "hostname", None):
        pod_spec["hostname"] = str(m.spec.hostname)
    if getattr(m.spec, "subdomain", None):
        pod_spec["subdomain"] = str(m.spec.subdomain)
    if getattr(m.spec, "host_aliases", None):
        aliases = []
        for ha in m.spec.host_aliases:
            if isinstance(ha, dict):
                ip = ha.get("ip")
                hns = ha.get("hostnames") or []
            else:
                ip = getattr(ha, "ip", None)
                hns = list(getattr(ha, "hostnames", []) or [])
            if ip:
                aliases.append({"ip": str(ip), "hostnames": list(hns)})
        if aliases:
            pod_spec["hostAliases"] = aliases
    if getattr(m.spec, "enable_service_links", None) is not None:
        pod_spec["enableServiceLinks"] = bool(m.spec.enable_service_links)
    if getattr(m.spec, "share_process_namespace", None) is not None:
        pod_spec["shareProcessNamespace"] = bool(m.spec.share_process_namespace)
    if getattr(m.spec, "host_network", None) is not None:
        pod_spec["hostNetwork"] = bool(m.spec.host_network)
    if getattr(m.spec, "node_selector", None):
        pod_spec["nodeSelector"] = dict(m.spec.node_selector)
    if getattr(m.spec, "set_hostname_as_fqdn", None) is not None:
        pod_spec["setHostnameAsFQDN"] = bool(m.spec.set_hostname_as_fqdn)
    if getattr(m.spec, "host_pid", None) is not None:
        pod_spec["hostPID"] = bool(m.spec.host_pid)
    if getattr(m.spec, "host_ipc", None) is not None:
        pod_spec["hostIPC"] = bool(m.spec.host_ipc)
    if getattr(m.spec, "host_pid", None) is not None:
        pod_spec["hostPID"] = bool(m.spec.host_pid)
    if getattr(m.spec, "host_ipc", None) is not None:
        pod_spec["hostIPC"] = bool(m.spec.host_ipc)
    if opts.service_account_name:
        pod_spec["serviceAccountName"] = opts.service_account_name
    # ImagePullSecrets
    if getattr(m.spec, "image_pull_secrets", None):
        pod_spec["imagePullSecrets"] = [{"name": s} for s in m.spec.image_pull_secrets]
    if getattr(m.spec, "affinity", None):
        pod_spec["affinity"] = dict(m.spec.affinity)
    if getattr(m.spec, "tolerations", None):
        pod_spec["tolerations"] = list(m.spec.tolerations)
    if getattr(m.spec, "topology_spread_constraints", None):
        pod_spec["topologySpreadConstraints"] = list(m.spec.topology_spread_constraints)
    elif opts.inject_topology_spread and int(getattr(m.spec, "replicas", 1) or 1) > 1:
        pod_spec["topologySpreadConstraints"] = [
            {
                "maxSkew": 1,
                "topologyKey": "kubernetes.io/hostname",
                "whenUnsatisfiable": "ScheduleAnyway",
                "labelSelector": {"matchLabels": _selector_labels(m)},
            }
        ]
    # DNS policy/config
    if getattr(m.spec, "dns_policy", None):
        pod_spec["dnsPolicy"] = str(m.spec.dns_policy)
    if getattr(m.spec, "dns_config", None):
        dnsc: Dict[str, Any] = {}
        dc = m.spec.dns_config
        if isinstance(dc, dict):
            if dc.get("nameservers"):
                dnsc["nameservers"] = list(dc.get("nameservers", []))
            if dc.get("searches"):
                dnsc["searches"] = list(dc.get("searches", []))
            opt_list = []
            for o in dc.get("options", []) or []:
                name = o.get("name")
                if not name:
                    continue
                ent = {"name": name}
                if o.get("value") is not None:
                    ent["value"] = str(o.get("value"))
                opt_list.append(ent)
            if opt_list:
                dnsc["options"] = opt_list
        else:
            if getattr(dc, "nameservers", None):
                dnsc["nameservers"] = list(dc.nameservers)
            if getattr(dc, "searches", None):
                dnsc["searches"] = list(dc.searches)
            opt_list = []
            for o in getattr(dc, "options", []) or []:
                ent = {"name": o.name}
                if getattr(o, "value", None) is not None:
                    ent["value"] = str(o.value)
                opt_list.append(ent)
            if opt_list:
                dnsc["options"] = opt_list
        if dnsc:
            pod_spec["dnsConfig"] = dnsc
    # Pod-level securityContext
    if getattr(m.spec, "pod_security", None):
        psc = {}
        if getattr(m.spec.pod_security, "fs_group", None) is not None:
            psc["fsGroup"] = int(m.spec.pod_security.fs_group)
        if getattr(m.spec.pod_security, "seccomp_type", None):
            sec = {"type": str(m.spec.pod_security.seccomp_type)}
            if str(m.spec.pod_security.seccomp_type) == "Localhost" and getattr(
                m.spec.pod_security, "seccomp_localhost_profile", None
            ):
                sec["localhostProfile"] = str(m.spec.pod_security.seccomp_localhost_profile)
            psc["seccompProfile"] = sec
        selinux = {}
        if getattr(m.spec.pod_security, "selinux_user", None):
            selinux["user"] = str(m.spec.pod_security.selinux_user)
        if getattr(m.spec.pod_security, "selinux_role", None):
            selinux["role"] = str(m.spec.pod_security.selinux_role)
        if getattr(m.spec.pod_security, "selinux_type", None):
            selinux["type"] = str(m.spec.pod_security.selinux_type)
        if getattr(m.spec.pod_security, "selinux_level", None):
            selinux["level"] = str(m.spec.pod_security.selinux_level)
        if selinux:
            psc["seLinuxOptions"] = selinux
        if psc:
            pod_spec["securityContext"] = psc

    # Storage via volumeClaimTemplates; mount by template name
    volume_mounts: List[Dict[str, Any]] = []
    vcts: List[Dict[str, Any]] = []
    # Projected volume from config/secret file projections
    proj = _projected_volume_from_refs(m)
    volume_specs: List[Dict[str, Any]] = []
    if proj is not None:
        volume_specs.append(proj)
        volume_mounts.append({"name": proj["name"], "mountPath": "/var/run/ae/config"})
    explicit = _explicit_volumes_from_refs(m)
    if explicit:
        volume_specs.extend(explicit)
    if opts.emit_storage and getattr(m.spec, "storage", None):
        for s in m.spec.storage:
            s_name = getattr(s, "name", None) if not isinstance(s, dict) else s.get("name")
            s_mount = (
                getattr(s, "mount_path", None)
                if not isinstance(s, dict)
                else (s.get("mountPath") or s.get("mount_path"))
            )
            if not s_name or not s_mount:
                continue
            claim = _storage_claim_name(m.metadata.name, s_name)
            req_size = (
                getattr(s, "size", None) if not isinstance(s, dict) else s.get("size")
            ) or opts.default_pvc_size
            access_modes = _storage_access_modes(s, opts)
            storage_class = _storage_class_name(s, opts)
            volume_mode = _storage_volume_mode(s)
            spec: Dict[str, Any] = {
                "accessModes": access_modes,
                "resources": {"requests": {"storage": str(req_size)}},
            }
            if storage_class:
                spec["storageClassName"] = storage_class
            if volume_mode:
                spec["volumeMode"] = volume_mode
            vcts.append(
                {
                    "metadata": {"name": claim},
                    "spec": spec,
                }
            )
            vm = {"name": claim, "mountPath": s_mount}
            if _storage_read_only(s):
                vm["readOnly"] = True
            volume_mounts.append(vm)
    if volume_mounts:
        for c in pod_spec.get("containers", []) or []:
            c.setdefault("volumeMounts", []).extend(volume_mounts)
    if volume_specs:
        pod_spec["volumes"] = (pod_spec.get("volumes") or []) + volume_specs

    labels = _resource_labels(m)
    selector = _selector_labels(m)
    pod_meta: Dict[str, Any] = {"labels": dict(labels)}
    if getattr(m.spec, "security", None) and getattr(m.spec.security, "apparmor_profile", None):
        ann_key = f"container.apparmor.security.beta.kubernetes.io/{m.metadata.name}"
        pod_meta["annotations"] = {ann_key: str(m.spec.security.apparmor_profile)}

    sts: Dict[str, Any] = {
        "apiVersion": "apps/v1",
        "kind": "StatefulSet",
        "metadata": {
            "name": m.metadata.name,
            "namespace": _resolve_namespace(m, opts),
            "labels": labels,
        },
        "spec": {
            "serviceName": f"{m.metadata.name}-headless",
            "replicas": int(m.spec.replicas),
            "selector": {"matchLabels": selector},
            "template": {"metadata": pod_meta, "spec": pod_spec},
            "updateStrategy": {"type": "RollingUpdate"},
        },
    }
    if getattr(m.spec, "init_containers", None):
        proj_name = proj["name"] if proj is not None else None
        sts["spec"]["template"]["spec"]["initContainers"] = [
            _container_from_spec(
                m, csp, opts=opts, allow_probes=False, projected_volume_name=proj_name
            )
            for csp in m.spec.init_containers
        ]
    if vcts:
        # Apply storageClassName/accessModes overrides when requested (override defaults)
        for tmpl in vcts:
            spec = tmpl.setdefault("spec", {})
            if opts.storage_class_name is not None:
                spec["storageClassName"] = str(opts.storage_class_name)
            if opts.pvc_access_modes is not None:
                spec["accessModes"] = list(opts.pvc_access_modes)
        sts["spec"]["volumeClaimTemplates"] = vcts
    return sts


def _ingress_from_manifest(m: AppManifest, opts: ExportOptions) -> Optional[Dict[str, Any]]:
    ing = m.spec.ingress
    if not ing:
        return None
    path_list = list(getattr(ing, "paths", []) or [])
    if not path_list:
        path_list = [ing.path or "/"]
    # Determine which Service port number to route HTTP to
    backend_number: int
    if getattr(m.spec, "service", None) and getattr(m.spec.service, "ports", None):
        # Prefer a port named 'http'; otherwise first declared service port
        http_port = next((p for p in m.spec.service.ports if str(p.name).lower() == "http"), None)
        if http_port is not None:
            backend_number = int(http_port.port)
        else:
            backend_number = int(m.spec.service.ports[0].port)
    else:
        backend_number = int(
            (
                opts.service_port
                or (
                    m.spec.service.port
                    if m.spec.service and m.spec.service.port is not None
                    else 80
                )
            )
        )
    # Validate/choose pathType
    path_type = opts.ingress_path_type or "Prefix"
    if opts.ingress_class_name:
        cls = str(opts.ingress_class_name).lower()
        if cls.find("traefik") != -1 and path_type not in {"Prefix", "ImplementationSpecific"}:
            raise ValueError("Traefik ingress supports Prefix/ImplementationSpecific pathType")
        # nginx is flexible; keep provided value
    k8s_paths = [
        {
            "path": p or "/",
            "pathType": path_type,
            "backend": {"service": {"name": m.metadata.name, "port": {"number": backend_number}}},
        }
        for p in path_list
    ]
    spec: Dict[str, Any] = {"rules": [{"host": ing.host, "http": {"paths": k8s_paths}}]}
    if opts.ingress_class_name:
        spec["ingressClassName"] = opts.ingress_class_name
    if getattr(ing, "tls", True):
        tls_entry: Dict[str, Any] = {"hosts": [ing.host]}
        secret = getattr(ing, "tls_secret_name", None)
        if secret:
            tls_entry["secretName"] = secret
        spec["tls"] = [tls_entry]
    meta: Dict[str, Any] = {
        "name": m.metadata.name,
        "namespace": _resolve_namespace(m, opts),
        "labels": _resource_labels(m),
    }
    if opts.ingress_annotations:
        meta.setdefault("annotations", {}).update(dict(opts.ingress_annotations))
    ing_res = {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "Ingress",
        "metadata": meta,
        "spec": spec,
    }
    return ing_res


def _configmap_from_ref(app: AppManifest, ref, opts: ExportOptions) -> Dict[str, Any]:
    data = {}
    if opts.inline_configs:
        try:
            import json as _json
            from pathlib import Path as _P

            content = _P(ref.path).read_text(encoding="utf-8")
            try:
                parsed = _json.loads(content)
            except _json.JSONDecodeError:
                import yaml as _yaml

                parsed = _yaml.safe_load(content)
            if isinstance(parsed, dict):
                data = {str(k): str(v) for k, v in parsed.items()}
        except Exception:
            data = {}
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": ref.name,
            "namespace": _resolve_namespace(app, opts),
            "labels": _resource_labels(app),
        },
        "data": data or None,
    }


def _secret_from_ref(app: AppManifest, ref, opts: ExportOptions) -> Dict[str, Any]:
    body: Dict[str, Any] = {"type": "Opaque"}
    if opts.inline_secrets:
        try:
            import json as _json
            from pathlib import Path as _P

            content = _P(ref.path).read_text(encoding="utf-8")
            try:
                parsed = _json.loads(content)
            except _json.JSONDecodeError:
                import yaml as _yaml

                parsed = _yaml.safe_load(content)
            if isinstance(parsed, dict):
                body["stringData"] = {str(k): str(v) for k, v in parsed.items()}
        except Exception:
            pass
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": ref.name,
            "namespace": _resolve_namespace(app, opts),
            "labels": _resource_labels(app),
        },
        **body,
    }


def _projected_volume_from_refs(app: AppManifest) -> Optional[Dict[str, Any]]:
    """Build a single projected volume aggregating config/secret file projections.

    Each file mapping is expressed via per-ref items with explicit path.
    The volume is mounted by callers at /var/run/ae/config.
    """
    sources: List[Dict[str, Any]] = []

    # group items per ref for compactness
    def _items_from(files: list[dict]) -> List[Dict[str, str]]:
        items: List[Dict[str, str]] = []
        for f in files or []:
            key = str(f.get("key", "")).strip()
            path = str(f.get("file", "")).lstrip("/")
            if not key or not path:
                continue
            items.append({"key": key, "path": path})
        return items

    # ConfigMaps
    for ref in getattr(app.spec, "config_refs", []) or []:
        items = _items_from(getattr(ref, "files", []) or [])
        if items:
            sources.append({"configMap": {"name": ref.name, "items": items}})
    # Secrets
    for ref in getattr(app.spec, "secret_refs", []) or []:
        items = _items_from(getattr(ref, "files", []) or [])
        if items:
            sources.append({"secret": {"name": ref.name, "items": items}})
    if not sources:
        return None
    return {"name": f"{app.metadata.name}-proj", "projected": {"sources": sources}}


def _explicit_volumes_from_refs(app: AppManifest) -> List[Dict[str, Any]]:
    """Emit explicit ConfigMap/Secret volumes with items when files[] present.

    Backward-compat: we still keep the single projected volume; these are additive.
    """
    vols: List[Dict[str, Any]] = []

    def _items(files: list[dict]) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for f in files or []:
            key = str(f.get("key", "")).strip()
            path = str(f.get("file", "")).lstrip("/")
            if not key or not path:
                continue
            ent: Dict[str, Any] = {"key": key, "path": path}
            if f.get("mode") is not None:
                try:
                    ent["mode"] = int(f.get("mode"))
                except Exception:
                    pass
            out.append(ent)
        return out

    for ref in getattr(app.spec, "config_refs", []) or []:
        items = _items(getattr(ref, "files", []) or [])
        if items:
            vols.append(
                {
                    "name": f"{app.metadata.name}-cfg-{ref.name}",
                    "configMap": {"name": ref.name, "items": items},
                }
            )
    for ref in getattr(app.spec, "secret_refs", []) or []:
        items = _items(getattr(ref, "files", []) or [])
        if items:
            vols.append(
                {
                    "name": f"{app.metadata.name}-sec-{ref.name}",
                    "secret": {"secretName": ref.name, "items": items},
                }
            )
    return vols


def _pvc_from_storage(app: AppManifest, s, opts: ExportOptions) -> Dict[str, Any]:
    s_name = getattr(s, "name", None) if not isinstance(s, dict) else s.get("name")
    s_size = getattr(s, "size", None) if not isinstance(s, dict) else s.get("size")
    size = s_size or opts.default_pvc_size
    access_modes = _storage_access_modes(s, opts)
    storage_class = _storage_class_name(s, opts)
    volume_mode = _storage_volume_mode(s)
    pvc = {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {
            "name": _storage_claim_name(app.metadata.name, s_name or "data"),
            "namespace": _resolve_namespace(app, opts),
            "labels": _resource_labels(app),
        },
        "spec": {
            "accessModes": access_modes,
            "resources": {"requests": {"storage": str(size)}},
        },
    }
    if storage_class:
        pvc["spec"]["storageClassName"] = storage_class
    if volume_mode:
        pvc["spec"]["volumeMode"] = volume_mode
    return pvc


def export_k8s_docs(
    manifest: AppManifest, *, options: Optional[ExportOptions] = None
) -> List[Dict[str, Any]]:
    """Produce a list of K8s resource dicts from a manifest."""
    opts = options or ExportOptions()
    ns = _resolve_namespace(manifest, opts)
    labels = _resource_labels(manifest)
    docs: List[Dict[str, Any]] = []
    wk = (opts.workload_kind or "Deployment").lower()
    # Strict requests gating (opt-in)
    if opts.require_requests:
        res = getattr(manifest.spec, "resources", None)
        req = getattr(res, "requests", None) if res else None
        has_cpu = bool(req and getattr(req, "cpu", None))
        has_mem = bool(req and getattr(req, "memory", None))
        if not (has_cpu and has_mem):
            raise ValueError(
                "resources.requests (cpu and memory) are required for export; remove --require-requests to allow best-effort export"
            )
    # Optional Namespace emission (so it exists before other objects when applying whole file)
    if bool(getattr(opts, "emit_namespace", False)):
        ns_meta = {"name": ns}
        labels: Dict[str, Any] = {}
        pse = getattr(opts, "pod_security_enforce", None) or None
        if pse:
            labels["pod-security.kubernetes.io/enforce"] = str(pse)
            # default to latest policy version
            labels["pod-security.kubernetes.io/enforce-version"] = "latest"
        if labels:
            ns_meta["labels"] = labels
        docs.append({"apiVersion": "v1", "kind": "Namespace", "metadata": ns_meta})

    # Optional resources first (so references exist when applying whole file)
    if opts.emit_configs:
        for ref in getattr(manifest.spec, "config_refs", []) or []:
            docs.append(_configmap_from_ref(manifest, ref, opts))
    if opts.emit_secrets:
        for ref in getattr(manifest.spec, "secret_refs", []) or []:
            docs.append(_secret_from_ref(manifest, ref, opts))
    if wk != "statefulset" and opts.emit_storage and getattr(manifest.spec, "storage", None):
        # For Deployments we emit standalone PVCs. StatefulSets use volumeClaimTemplates.
        for s in manifest.spec.storage:
            docs.append(_pvc_from_storage(manifest, s, opts))

    # Workload
    if wk == "statefulset":
        headless = _headless_service_for_statefulset(manifest, opts)
        if headless is not None:
            docs.append(headless)
        docs.append(_statefulset_from_manifest(manifest, opts))
    elif wk == "job":
        docs.append(_job_from_manifest(manifest, opts))
    elif wk == "cronjob":
        docs.append(_cronjob_from_manifest(manifest, opts))
    else:
        docs.append(_deployment_from_manifest(manifest, opts))

    # Service/Ingress: skip for Job/CronJob by default
    if wk not in {"job", "cronjob"}:
        svc = _service_from_manifest(manifest, opts)
        if svc is not None:
            docs.append(svc)
        ing = _ingress_from_manifest(manifest, opts)
        if ing is not None:
            docs.append(ing)
    # Optional ServiceAccount
    if opts.service_account_name:
        docs.append(
            {
                "apiVersion": "v1",
                "kind": "ServiceAccount",
                "metadata": {
                    "name": opts.service_account_name,
                    "namespace": ns,
                    "labels": labels,
                },
            }
        )
        # Emit a minimal Namespaced Role and RoleBinding tied to this ServiceAccount.
        # Conservative, read-only permissions useful for basic app diagnostics.
        role_name = f"{manifest.metadata.name}-role"
        rb_name = f"{manifest.metadata.name}-rb"
        docs.append(
            {
                "apiVersion": "rbac.authorization.k8s.io/v1",
                "kind": "Role",
                "metadata": {"name": role_name, "namespace": ns, "labels": labels},
                "rules": [
                    {  # Core read access to pods/services/endpoints/events (no secrets)
                        "apiGroups": [""],
                        "resources": [
                            "pods",
                            "pods/log",
                            "services",
                            "endpoints",
                            "events",
                            "configmaps",
                        ],
                        "verbs": ["get", "list", "watch"],
                    }
                ],
            }
        )
        docs.append(
            {
                "apiVersion": "rbac.authorization.k8s.io/v1",
                "kind": "RoleBinding",
                "metadata": {"name": rb_name, "namespace": ns, "labels": labels},
                "subjects": [
                    {"kind": "ServiceAccount", "name": opts.service_account_name, "namespace": ns}
                ],
                "roleRef": {
                    "apiGroup": "rbac.authorization.k8s.io",
                    "kind": "Role",
                    "name": role_name,
                },
            }
        )
    # Optional PodDisruptionBudget (not applicable to Job/CronJob)
    if wk not in {"job", "cronjob"} and opts.emit_pdb and int(manifest.spec.replicas) > 1:
        # Choose either minAvailable or maxUnavailable; prefer explicit provided one.
        spec_pdb: Dict[str, Any] = {"selector": {"matchLabels": _selector_labels(manifest)}}
        if opts.pdb_max_unavailable is not None and opts.pdb_min_available is not None:
            raise ValueError(
                "PDB minAvailable and maxUnavailable are mutually exclusive; provide only one"
            )
        if opts.pdb_max_unavailable is not None:
            try:
                spec_pdb["maxUnavailable"] = int(opts.pdb_max_unavailable)
            except Exception:
                spec_pdb["maxUnavailable"] = str(opts.pdb_max_unavailable)
        else:
            if opts.pdb_min_available is not None:
                try:
                    spec_pdb["minAvailable"] = int(opts.pdb_min_available)
                except Exception:
                    spec_pdb["minAvailable"] = str(opts.pdb_min_available)
            else:
                spec_pdb["minAvailable"] = 1
        docs.append(
            {
                "apiVersion": "policy/v1",
                "kind": "PodDisruptionBudget",
                "metadata": {
                    "name": f"{manifest.metadata.name}-pdb",
                    "namespace": ns,
                    "labels": labels,
                },
                "spec": spec_pdb,
            }
        )
    # Optional HPA (autoscaling/v2)
    if (
        opts.hpa_min is not None
        and opts.hpa_max is not None
        and (
            opts.hpa_cpu_target is not None
            or opts.hpa_mem_target is not None
            or (opts.hpa_mem_type == "value" and bool(opts.hpa_mem_value))
        )
    ):
        # Preconditions: CPU utilization requires resources.requests.cpu; memory utilization requires resources.requests.memory
        # AverageValue for memory does not require requests.
        res = getattr(manifest.spec, "resources", None)
        req = getattr(res, "requests", None) if res else None
        cpu_req = bool(req and getattr(req, "cpu", None))
        mem_req = bool(req and getattr(req, "memory", None))
        if not opts.allow_hpa_without_requests:
            if opts.hpa_cpu_target is not None and not cpu_req:
                raise ValueError(
                    "HPA CPU utilization requires resources.requests.cpu; set requests or use --allow-hpa-no-requests"
                )
            if (
                opts.hpa_mem_target is not None
                and not mem_req
                and (opts.hpa_mem_type or "utilization") == "utilization"
            ):
                raise ValueError(
                    "HPA memory utilization requires resources.requests.memory; set requests or use --allow-hpa-no-requests"
                )
        metrics: List[Dict[str, Any]] = []
        if opts.hpa_cpu_target is not None:
            metrics.append(
                {
                    "type": "Resource",
                    "resource": {
                        "name": "cpu",
                        "target": {
                            "type": "Utilization",
                            "averageUtilization": int(opts.hpa_cpu_target),
                        },
                    },
                }
            )
        if opts.hpa_mem_target is not None or (opts.hpa_mem_type == "value" and opts.hpa_mem_value):
            if opts.hpa_mem_type == "value":
                metrics.append(
                    {
                        "type": "Resource",
                        "resource": {
                            "name": "memory",
                            "target": {
                                "type": "AverageValue",
                                "averageValue": str(opts.hpa_mem_value),
                            },
                        },
                    }
                )
            else:
                metrics.append(
                    {
                        "type": "Resource",
                        "resource": {
                            "name": "memory",
                            "target": {
                                "type": "Utilization",
                                "averageUtilization": int(opts.hpa_mem_target or 0),
                            },
                        },
                    }
                )
        scale_kind = "StatefulSet" if wk == "statefulset" else "Deployment"
        docs.append(
            {
                "apiVersion": "autoscaling/v2",
                "kind": "HorizontalPodAutoscaler",
                "metadata": {
                    "name": f"{manifest.metadata.name}",
                    "namespace": ns,
                    "labels": labels,
                },
                "spec": {
                    "scaleTargetRef": {
                        "apiVersion": "apps/v1",
                        "kind": scale_kind,
                        "name": manifest.metadata.name,
                    },
                    "minReplicas": int(opts.hpa_min),
                    "maxReplicas": int(opts.hpa_max),
                    "metrics": metrics,
                    **(
                        {
                            "behavior": {
                                **(
                                    {"scaleUp": dict(opts.hpa_behavior_up)}
                                    if opts.hpa_behavior_up
                                    else {}
                                ),
                                **(
                                    {"scaleDown": dict(opts.hpa_behavior_down)}
                                    if opts.hpa_behavior_down
                                    else {}
                                ),
                            }
                        }
                        if (opts.hpa_behavior_up or opts.hpa_behavior_down)
                        else {}
                    ),
                },
            }
        )
    # NetworkPolicy: explicit from manifest or generated default
    if getattr(manifest.spec, "network_policy", None):
        np = _network_policy_from_manifest(manifest, opts)
        if np:
            docs.append(np)
    elif opts.emit_network_policy and (opts.np_default_deny_ingress or opts.np_default_deny_egress):
        np = _default_network_policy(manifest, opts)
        if np:
            docs.append(np)
    return docs


def _pod_template_from_manifest(m: AppManifest, opts: ExportOptions) -> Dict[str, Any]:
    """Return a Deployment-style Pod template for reuse in Job/CronJob."""
    dep = _deployment_from_manifest(m, opts)
    return dep["spec"]["template"]


def _job_from_manifest(m: AppManifest, opts: ExportOptions) -> Dict[str, Any]:
    tpl = _pod_template_from_manifest(m, opts)
    # Ensure a valid restartPolicy for Jobs
    tpl.setdefault("spec", {}).setdefault("restartPolicy", "OnFailure")
    body: Dict[str, Any] = {"template": tpl}
    if opts.job_backoff_limit is not None:
        body["backoffLimit"] = int(opts.job_backoff_limit)
    if opts.job_ttl_seconds_after_finished is not None:
        body["ttlSecondsAfterFinished"] = int(opts.job_ttl_seconds_after_finished)
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": m.metadata.name,
            "namespace": _resolve_namespace(m, opts),
            "labels": _resource_labels(m),
        },
        "spec": body,
    }


def _cronjob_from_manifest(m: AppManifest, opts: ExportOptions) -> Dict[str, Any]:
    if not opts.cron_schedule:
        raise ValueError("CronJob requires --cron-schedule (e.g., '*/5 * * * *')")
    tpl = _pod_template_from_manifest(m, opts)
    tpl.setdefault("spec", {}).setdefault("restartPolicy", "OnFailure")
    job_spec: Dict[str, Any] = {"template": tpl}
    if opts.job_backoff_limit is not None:
        job_spec["backoffLimit"] = int(opts.job_backoff_limit)
    if opts.job_ttl_seconds_after_finished is not None:
        job_spec["ttlSecondsAfterFinished"] = int(opts.job_ttl_seconds_after_finished)
    spec: Dict[str, Any] = {
        "schedule": str(opts.cron_schedule),
        "jobTemplate": {"spec": job_spec},
    }
    if opts.cron_concurrency_policy is not None:
        spec["concurrencyPolicy"] = str(opts.cron_concurrency_policy)
    if opts.cron_suspend is not None:
        spec["suspend"] = bool(opts.cron_suspend)
    if opts.cron_starting_deadline_seconds is not None:
        spec["startingDeadlineSeconds"] = int(opts.cron_starting_deadline_seconds)
    return {
        "apiVersion": "batch/v1",
        "kind": "CronJob",
        "metadata": {
            "name": m.metadata.name,
            "namespace": _resolve_namespace(m, opts),
            "labels": _resource_labels(m),
        },
        "spec": spec,
    }


def _network_policy_from_manifest(m: AppManifest, opts: ExportOptions) -> Optional[dict]:
    spec = getattr(m.spec, "network_policy", None)
    if not isinstance(spec, dict):
        return None
    policy_types = list(spec.get("policyTypes") or [])
    ingress = list(spec.get("ingress") or [])
    egress = list(spec.get("egress") or [])
    labels = _resource_labels(m)
    pod_selector = spec.get("podSelector") or {"matchLabels": _selector_labels(m)}
    body = {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {
            "name": m.metadata.name,
            "namespace": _resolve_namespace(m, opts),
            "labels": labels,
        },
        "spec": {"podSelector": pod_selector},
    }
    if policy_types:
        body["spec"]["policyTypes"] = policy_types
    if ingress:
        body["spec"]["ingress"] = ingress
    if egress:
        body["spec"]["egress"] = egress
    return body


def _default_network_policy(m: AppManifest, opts: ExportOptions) -> dict:
    policy_types = []
    if opts.np_default_deny_ingress:
        policy_types.append("Ingress")
    if opts.np_default_deny_egress:
        policy_types.append("Egress")
    spec: Dict[str, Any] = {
        "podSelector": {"matchLabels": _selector_labels(m)},
        "policyTypes": policy_types or ["Ingress"],
    }
    # Default deny is achieved by providing no rules for selected types
    if opts.np_default_deny_ingress:
        spec["ingress"] = []
    if opts.np_default_deny_egress:
        egress: List[Dict[str, Any]] = []
        # Optionally allow DNS egress (TCP/UDP 53) to anywhere
        if opts.np_allow_dns:
            egress.append(
                {
                    "to": [{"ipBlock": {"cidr": "0.0.0.0/0"}}],
                    "ports": [
                        {"protocol": "UDP", "port": 53},
                        {"protocol": "TCP", "port": 53},
                    ],
                }
            )
        # Optionally allow HTTP/HTTPS egress (TCP 80/443)
        if opts.np_allow_web:
            egress.append(
                {
                    "to": [{"ipBlock": {"cidr": "0.0.0.0/0"}}],
                    "ports": [
                        {"protocol": "TCP", "port": 80},
                        {"protocol": "TCP", "port": 443},
                    ],
                }
            )
        # Optionally allow internal RFC1918 destinations for selected ports
        if opts.np_allow_internal_ports:
            private_v4 = [
                {"cidr": "10.0.0.0/8"},
                {"cidr": "172.16.0.0/12"},
                {"cidr": "192.168.0.0/16"},
            ]
            for p in opts.np_allow_internal_ports:
                try:
                    portnum = int(p)
                except Exception:
                    continue
                egress.append(
                    {
                        "to": [{"ipBlock": cidr} for cidr in private_v4],
                        "ports": [{"protocol": "TCP", "port": portnum}],
                    }
                )
        spec["egress"] = egress
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {
            "name": m.metadata.name,
            "namespace": _resolve_namespace(m, opts),
            "labels": _resource_labels(m),
        },
        "spec": spec,
    }


def export_k8s_yaml(manifest: AppManifest, *, options: Optional[ExportOptions] = None) -> str:
    """Render a multi-document YAML string for the manifest's K8s resources."""
    docs = export_k8s_docs(manifest, options=options)
    parts = []
    for d in docs:
        parts.append(
            yaml.safe_dump(d, sort_keys=False, default_flow_style=False, indent=2).rstrip()
        )
    return "\n---\n".join(parts) + "\n"


# ruff: noqa
# ruff: noqa: E501,UP006,UP007,UP017,UP035,S110,S112,SIM102,SIM105,B009,ARG001,ARG002
