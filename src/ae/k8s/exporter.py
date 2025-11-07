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

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import yaml

from ae.controller.spec import AppManifest


@dataclass(slots=True)
class ExportOptions:
    # Which workload to emit
    workload_kind: str = "Deployment"  # "Deployment" or "StatefulSet"
    namespace: str = "default"
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
    # Security defaults
    default_security: bool = False
    # PDB tuning
    pdb_min_available: Optional[int] = None
    pdb_max_unavailable: Optional[int] = None
    # Strictness
    require_requests: bool = False


def _container_from_manifest(m: AppManifest, *, opts: ExportOptions) -> Dict[str, Any]:
    spec = m.spec
    c: Dict[str, Any] = {
        "name": m.metadata.name,
        "image": spec.image,
    }
    if spec.command:
        c["command"] = list(spec.command)
    # env: start with explicit pairs
    env: List[Dict[str, Any]] = []
    for item in spec.env:
        name = item.get("name")
        value = item.get("value")
        if name is None:
            continue
        env.append({"name": name, "value": value if value is not None else ""})
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
    if env:
        c["env"] = env
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

    body = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": m.metadata.name, "namespace": opts.namespace},
        "spec": {
            "selector": {"app": m.metadata.name},
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
    return body


def _deployment_from_manifest(m: AppManifest, opts: ExportOptions) -> Dict[str, Any]:
    c = _container_from_manifest(m, opts=opts)
    pod_spec: Dict[str, Any] = {"containers": [c]}
    if m.spec.termination_grace_period_seconds is not None:
        pod_spec["terminationGracePeriodSeconds"] = int(m.spec.termination_grace_period_seconds)
    if getattr(m.spec, "priority_class_name", None):
        pod_spec["priorityClassName"] = str(m.spec.priority_class_name)
    # ServiceAccount
    if opts.service_account_name:
        pod_spec["serviceAccountName"] = opts.service_account_name
    # Scheduling pass-through
    if getattr(m.spec, "affinity", None):
        pod_spec["affinity"] = dict(m.spec.affinity)
    if getattr(m.spec, "tolerations", None):
        pod_spec["tolerations"] = list(m.spec.tolerations)
    if getattr(m.spec, "topology_spread_constraints", None):
        pod_spec["topologySpreadConstraints"] = list(m.spec.topology_spread_constraints)
    # Storage mounts
    volume_specs: List[Dict[str, Any]] = []
    volume_mounts: List[Dict[str, Any]] = []
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
            volume_mounts.append({"name": claim, "mountPath": s_mount})
    if volume_specs:
        pod_spec["volumes"] = volume_specs
    if volume_mounts:
        c.setdefault("volumeMounts", []).extend(volume_mounts)
    # Add AppArmor annotation on the Pod template when requested
    pod_meta: Dict[str, Any] = {"labels": {"app": m.metadata.name}}
    if getattr(m.spec, "security", None) and getattr(m.spec.security, "apparmor_profile", None):
        ann_key = f"container.apparmor.security.beta.kubernetes.io/{m.metadata.name}"
        pod_meta["annotations"] = {ann_key: str(m.spec.security.apparmor_profile)}

    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": m.metadata.name, "namespace": opts.namespace},
        "spec": {
            "replicas": int(m.spec.replicas),
            "selector": {"matchLabels": {"app": m.metadata.name}},
            "template": {
                "metadata": pod_meta,
                "spec": pod_spec,
            },
        },
    }


def _storage_claim_name(app: str, name: str) -> str:
    return f"{app}-{name}"


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
    spec: Dict[str, Any] = {"clusterIP": "None", "selector": {"app": m.metadata.name}}
    if ports:
        spec["ports"] = ports
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": name, "namespace": opts.namespace},
        "spec": spec,
    }


def _statefulset_from_manifest(m: AppManifest, opts: ExportOptions) -> Dict[str, Any]:
    c = _container_from_manifest(m, opts=opts)
    pod_spec: Dict[str, Any] = {"containers": [c]}
    if m.spec.termination_grace_period_seconds is not None:
        pod_spec["terminationGracePeriodSeconds"] = int(m.spec.termination_grace_period_seconds)
    if getattr(m.spec, "priority_class_name", None):
        pod_spec["priorityClassName"] = str(m.spec.priority_class_name)
    if opts.service_account_name:
        pod_spec["serviceAccountName"] = opts.service_account_name
    if getattr(m.spec, "affinity", None):
        pod_spec["affinity"] = dict(m.spec.affinity)
    if getattr(m.spec, "tolerations", None):
        pod_spec["tolerations"] = list(m.spec.tolerations)
    if getattr(m.spec, "topology_spread_constraints", None):
        pod_spec["topologySpreadConstraints"] = list(m.spec.topology_spread_constraints)

    # Storage via volumeClaimTemplates; mount by template name
    volume_mounts: List[Dict[str, Any]] = []
    vcts: List[Dict[str, Any]] = []
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
            vcts.append(
                {
                    "metadata": {"name": claim},
                    "spec": {
                        "accessModes": ["ReadWriteOnce"],
                        "resources": {"requests": {"storage": str(req_size)}},
                    },
                }
            )
            volume_mounts.append({"name": claim, "mountPath": s_mount})
    if volume_mounts:
        c.setdefault("volumeMounts", []).extend(volume_mounts)

    pod_meta: Dict[str, Any] = {"labels": {"app": m.metadata.name}}
    if getattr(m.spec, "security", None) and getattr(m.spec.security, "apparmor_profile", None):
        ann_key = f"container.apparmor.security.beta.kubernetes.io/{m.metadata.name}"
        pod_meta["annotations"] = {ann_key: str(m.spec.security.apparmor_profile)}

    sts: Dict[str, Any] = {
        "apiVersion": "apps/v1",
        "kind": "StatefulSet",
        "metadata": {"name": m.metadata.name, "namespace": opts.namespace},
        "spec": {
            "serviceName": f"{m.metadata.name}-headless",
            "replicas": int(m.spec.replicas),
            "selector": {"matchLabels": {"app": m.metadata.name}},
            "template": {"metadata": pod_meta, "spec": pod_spec},
            "updateStrategy": {"type": "RollingUpdate"},
        },
    }
    if vcts:
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
    k8s_paths = [
        {
            "path": p or "/",
            "pathType": "Prefix",
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
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "Ingress",
        "metadata": {"name": m.metadata.name, "namespace": opts.namespace},
        "spec": spec,
    }


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
            "namespace": opts.namespace,
            "labels": {"app": app.metadata.name},
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
            "namespace": opts.namespace,
            "labels": {"app": app.metadata.name},
        },
        **body,
    }


def _pvc_from_storage(app: AppManifest, s, opts: ExportOptions) -> Dict[str, Any]:
    s_name = getattr(s, "name", None) if not isinstance(s, dict) else s.get("name")
    s_size = getattr(s, "size", None) if not isinstance(s, dict) else s.get("size")
    size = s_size or opts.default_pvc_size
    return {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {
            "name": _storage_claim_name(app.metadata.name, s_name or "data"),
            "namespace": opts.namespace,
            "labels": {"app": app.metadata.name},
        },
        "spec": {
            "accessModes": ["ReadWriteOnce"],
            "resources": {"requests": {"storage": str(size)}},
        },
    }


def export_k8s_docs(
    manifest: AppManifest, *, options: Optional[ExportOptions] = None
) -> List[Dict[str, Any]]:
    """Produce a list of K8s resource dicts from a manifest."""
    opts = options or ExportOptions()
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
    else:
        docs.append(_deployment_from_manifest(manifest, opts))

    # Service when ports exist (normal ClusterIP for routing)
    svc = _service_from_manifest(manifest, opts)
    if svc is not None:
        docs.append(svc)
    # Ingress when requested
    ing = _ingress_from_manifest(manifest, opts)
    if ing is not None:
        docs.append(ing)
    # Optional ServiceAccount
    if opts.service_account_name:
        docs.append(
            {
                "apiVersion": "v1",
                "kind": "ServiceAccount",
                "metadata": {"name": opts.service_account_name, "namespace": opts.namespace},
            }
        )
    # Optional PodDisruptionBudget
    if opts.emit_pdb and int(manifest.spec.replicas) > 1:
        # Choose either minAvailable or maxUnavailable; prefer explicit provided one.
        spec_pdb: Dict[str, Any] = {"selector": {"matchLabels": {"app": manifest.metadata.name}}}
        if opts.pdb_max_unavailable is not None and opts.pdb_min_available is not None:
            raise ValueError(
                "PDB minAvailable and maxUnavailable are mutually exclusive; provide only one"
            )
        if opts.pdb_max_unavailable is not None:
            spec_pdb["maxUnavailable"] = int(opts.pdb_max_unavailable)
        else:
            spec_pdb["minAvailable"] = (
                int(opts.pdb_min_available) if opts.pdb_min_available is not None else 1
            )
        docs.append(
            {
                "apiVersion": "policy/v1",
                "kind": "PodDisruptionBudget",
                "metadata": {"name": f"{manifest.metadata.name}-pdb", "namespace": opts.namespace},
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
                "metadata": {"name": f"{manifest.metadata.name}", "namespace": opts.namespace},
                "spec": {
                    "scaleTargetRef": {
                        "apiVersion": "apps/v1",
                        "kind": scale_kind,
                        "name": manifest.metadata.name,
                    },
                    "minReplicas": int(opts.hpa_min),
                    "maxReplicas": int(opts.hpa_max),
                    "metrics": metrics,
                },
            }
        )
        # Optional NetworkPolicy from manifest.spec.networkPolicy
    if getattr(manifest.spec, "network_policy", None):
        np = _network_policy_from_manifest(manifest, opts)
        if np:
            docs.append(np)
    return docs


def _network_policy_from_manifest(m: AppManifest, opts: ExportOptions) -> Optional[dict]:
    spec = getattr(m.spec, "network_policy", None)
    if not isinstance(spec, dict):
        return None
    policy_types = list(spec.get("policyTypes") or [])
    ingress = list(spec.get("ingress") or [])
    egress = list(spec.get("egress") or [])
    pod_selector = spec.get("podSelector") or {"matchLabels": {"app": m.metadata.name}}
    body = {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {"name": m.metadata.name, "namespace": opts.namespace},
        "spec": {"podSelector": pod_selector},
    }
    if policy_types:
        body["spec"]["policyTypes"] = policy_types
    if ingress:
        body["spec"]["ingress"] = ingress
    if egress:
        body["spec"]["egress"] = egress
    return body


def export_k8s_yaml(manifest: AppManifest, *, options: Optional[ExportOptions] = None) -> str:
    """Render a multi-document YAML string for the manifest's K8s resources."""
    docs = export_k8s_docs(manifest, options=options)
    parts = []
    for d in docs:
        parts.append(
            yaml.safe_dump(d, sort_keys=False, default_flow_style=False, indent=2).rstrip()
        )
    return "\n---\n".join(parts) + "\n"
