# ruff: noqa: E501
"""Kubernetes manifest conversion helpers for k1s."""

from __future__ import annotations

import os
from collections.abc import Iterable
from typing import Any

from ae.controller.spec import (
    DEFAULT_NAMESPACE,
    AppManifest,
    AppSpec,
    IngressSpec,
    Metadata,
    PortSpec,
    ServiceSpec,
    app_key,
)


def app_name_for_k8s(namespace: str | None, name: str) -> str:
    return app_key(name, namespace)


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _metadata(obj: Any) -> dict[str, Any]:
    meta = _get(obj, "metadata", {}) or {}
    return meta if isinstance(meta, dict) else {}


def _spec(obj: Any) -> dict[str, Any]:
    spec = _get(obj, "spec", {}) or {}
    return spec if isinstance(spec, dict) else {}


def _name(obj: Any) -> str:
    meta = _metadata(obj)
    return str(_get(obj, "name", meta.get("name") or "") or "")


def _namespace(obj: Any) -> str | None:
    meta = _metadata(obj)
    ns = _get(obj, "namespace", meta.get("namespace"))
    return str(ns) if ns else None


def service_selector(spec: dict[str, Any]) -> dict[str, str]:
    raw = spec.get("selector") or {}
    selector: dict[str, Any] = raw if isinstance(raw, dict) else {}
    if (
        "matchLabels" in selector
        and isinstance(selector.get("matchLabels"), dict)
        and len(selector) == 1
    ):
        selector = selector.get("matchLabels") or {}
    if not selector:
        maybe = (
            (spec.get("selector") or {}).get("matchLabels")
            if isinstance(spec.get("selector"), dict)
            else None
        )
        if isinstance(maybe, dict):
            selector = maybe
    if not isinstance(selector, dict):
        return {}
    return {str(k): str(v) for k, v in selector.items()}


def pod_template_labels(obj: Any) -> dict[str, str]:
    spec = _spec(obj)
    template = spec.get("template") or {}
    meta = template.get("metadata") or {}
    labels = meta.get("labels") or {}
    if not isinstance(labels, dict):
        return {}
    return {str(k): str(v) for k, v in labels.items()}


def pod_template_ports_by_name(obj: Any) -> dict[str, int]:
    spec = _spec(obj)
    template = spec.get("template") or {}
    tpl_spec = template.get("spec") or {}
    ports_by_name: dict[str, int] = {}
    for container in tpl_spec.get("containers") or []:
        if not isinstance(container, dict):
            continue
        for port in container.get("ports") or []:
            if not isinstance(port, dict):
                continue
            name = port.get("name")
            if not name:
                continue
            try:
                port_val = int(port.get("containerPort"))
            except Exception:  # noqa: S112
                continue
            ports_by_name[str(name)] = port_val
    return ports_by_name


def selector_matches(selector: dict[str, str], labels: dict[str, str]) -> bool:
    if not selector:
        return False
    return all(labels.get(key) == val for key, val in selector.items())


def fallback_service_target(svc: Any, selector: dict[str, str]) -> str | None:
    meta = _metadata(svc)
    labels = meta.get("labels") if isinstance(meta, dict) else {}
    annotations = meta.get("annotations") if isinstance(meta, dict) else {}
    return (
        selector.get("app")
        or selector.get("app.kubernetes.io/name")
        or (labels.get("app") if isinstance(labels, dict) else None)
        or (annotations.get("apishim.k1s.dev/app") if isinstance(annotations, dict) else None)
        or _name(svc)
    )


def resolve_port_value(port: Any, ports_by_name: dict[str, int]) -> int | None:
    if port is None:
        return None
    if isinstance(port, int):
        return port
    if isinstance(port, str):
        if port.isdigit():
            return int(port)
        if port in ports_by_name:
            return ports_by_name[port]
    return None


def probe_from_k8s(raw: dict | None, ports_by_name: dict[str, int]) -> dict | None:
    if not raw or not isinstance(raw, dict):
        return None
    out: dict[str, Any] = {}
    http = raw.get("httpGet") or raw.get("http_get") or {}
    if isinstance(http, dict) and http:
        port = resolve_port_value(http.get("port"), ports_by_name)
        if port is not None:
            out["httpGet"] = {"path": http.get("path", "/"), "port": int(port)}
    exec_spec = raw.get("exec") or {}
    if isinstance(exec_spec, dict) and exec_spec.get("command"):
        out["exec"] = {"command": [str(x) for x in exec_spec.get("command") or []]}
    tcp = raw.get("tcpSocket") or raw.get("tcp_socket") or {}
    if isinstance(tcp, dict) and tcp:
        port = resolve_port_value(tcp.get("port"), ports_by_name)
        if port is not None:
            out["tcpSocket"] = {"port": int(port)}
    for key in (
        "initialDelaySeconds",
        "timeoutSeconds",
        "periodSeconds",
        "successThreshold",
        "failureThreshold",
    ):
        if key in raw:
            try:
                out[key] = int(raw.get(key))
            except Exception:  # noqa: S112
                continue
    return out or None


def manifest_from_k8s_workload(
    obj: Any,
    *,
    service_spec: ServiceSpec | None = None,
    ingress_spec: IngressSpec | None = None,
) -> AppManifest:
    spec: dict[str, Any] = _spec(obj)
    tpl = (spec.get("template") or {}).get("spec") or {}
    containers = tpl.get("containers") or []
    if not containers:
        c0: dict[str, Any] = {}
        image = "busybox:latest"
        ports: list[PortSpec] = []
    else:
        c0 = containers[0]
        image = c0.get("image") or "busybox:latest"
        ports = []
        for p in c0.get("ports") or []:
            try:
                port_num = int(p.get("containerPort"))
            except Exception:  # noqa: S112
                continue
            name = p.get("name") or f"p{port_num}"
            ports.append(PortSpec(name=name, containerPort=port_num))
    ports_by_name = {p.name: int(p.container_port) for p in ports if getattr(p, "name", None)}
    command = [str(x) for x in (c0.get("command") or [])]
    args = [str(x) for x in (c0.get("args") or [])]
    env: list[dict[str, str]] = []
    for item in c0.get("env") or []:
        if isinstance(item, dict) and "name" in item and "value" in item:
            env.append({"name": str(item["name"]), "value": str(item.get("value") or "")})
    working_dir = c0.get("workingDir")
    pvc_mounts: list[dict[str, Any]] = []
    host_mounts: list[dict[str, Any]] = []
    try:
        volume_claims: dict[str, tuple[str, bool]] = {}
        volume_hosts: dict[str, str] = {}
        for vol in tpl.get("volumes") or []:
            if not isinstance(vol, dict):
                continue
            vname = vol.get("name")
            pvc = vol.get("persistentVolumeClaim") or {}
            if not isinstance(pvc, dict):
                continue
            claim = pvc.get("claimName")
            if vname and claim:
                volume_claims[str(vname)] = (str(claim), bool(pvc.get("readOnly", False)))
            host = vol.get("hostPath")
            host_path = None
            if isinstance(host, dict):
                host_path = host.get("path")
            elif isinstance(host, str):
                host_path = host
            if vname and host_path:
                volume_hosts[str(vname)] = str(host_path)
        if volume_claims or volume_hosts:
            seen_pvc: set[tuple[str, str, bool]] = set()
            seen_hosts: set[tuple[str, str, bool]] = set()
            for container in containers or []:
                if not isinstance(container, dict):
                    continue
                for vm in container.get("volumeMounts") or []:
                    if not isinstance(vm, dict):
                        continue
                    vname = vm.get("name")
                    mount_path = vm.get("mountPath")
                    if not mount_path:
                        mount_path = None
                    entry = volume_claims.get(str(vname)) if vname else None
                    if entry and mount_path:
                        claim, vol_read_only = entry
                        read_only = bool(vm.get("readOnly", False)) or bool(vol_read_only)
                        sub_path = vm.get("subPath")
                        key = (str(claim), str(mount_path), bool(read_only))
                        if key not in seen_pvc:
                            seen_pvc.add(key)
                            record: dict[str, Any] = {
                                "claimName": str(claim),
                                "mountPath": str(mount_path),
                                "readOnly": read_only,
                            }
                            if sub_path:
                                record["subPath"] = str(sub_path)
                            pvc_mounts.append(record)
                    host_path = volume_hosts.get(str(vname)) if vname else None
                    if host_path and mount_path:
                        read_only = bool(vm.get("readOnly", False))
                        sub_path = vm.get("subPath")
                        if sub_path:
                            host_path = os.path.join(str(host_path), str(sub_path).lstrip("/"))
                        key = (str(host_path), str(mount_path), bool(read_only))
                        if key not in seen_hosts:
                            seen_hosts.add(key)
                            host_mounts.append(
                                {
                                    "hostPath": str(host_path),
                                    "mountPath": str(mount_path),
                                    "readOnly": read_only,
                                }
                            )
                for vd in container.get("volumeDevices") or []:
                    if not isinstance(vd, dict):
                        continue
                    vname = vd.get("name")
                    entry = volume_claims.get(str(vname)) if vname else None
                    if not entry:
                        continue
                    claim, vol_read_only = entry
                    device_path = vd.get("devicePath")
                    if not device_path:
                        continue
                    read_only = bool(vol_read_only)
                    key = (str(claim), str(device_path), bool(read_only))
                    if key in seen_pvc:
                        continue
                    seen_pvc.add(key)
                    pvc_mounts.append(
                        {
                            "claimName": str(claim),
                            "mountPath": str(device_path),
                            "devicePath": str(device_path),
                            "readOnly": read_only,
                        }
                    )
    except Exception:  # noqa: S112 - best-effort PVC extraction
        pvc_mounts = []
        host_mounts = []
    resources: dict[str, Any] | None = None
    if isinstance(c0.get("resources"), dict):
        res = c0.get("resources") or {}
        if res.get("requests") or res.get("limits"):
            resources = {}
            if res.get("requests"):
                req = res.get("requests") or {}
                resources["requests"] = {k: v for k, v in req.items() if k in {"cpu", "memory"}}
            if res.get("limits"):
                lim = res.get("limits") or {}
                resources["limits"] = {k: v for k, v in lim.items() if k in {"cpu", "memory"}}
            if resources.get("requests") == {}:
                resources.pop("requests", None)
            if resources.get("limits") == {}:
                resources.pop("limits", None)
            if not resources:
                resources = None
    security: dict[str, Any] | None = None
    sec = c0.get("securityContext") or {}
    if isinstance(sec, dict) and sec:
        security = {}
        if sec.get("runAsUser") is not None:
            security["run_as_user"] = sec.get("runAsUser")
        if sec.get("runAsGroup") is not None:
            security["run_as_group"] = sec.get("runAsGroup")
        if sec.get("readOnlyRootFilesystem") is not None:
            security["read_only_root"] = bool(sec.get("readOnlyRootFilesystem"))
        caps = (
            (sec.get("capabilities") or {}).get("drop")
            if isinstance(sec.get("capabilities"), dict)
            else None
        )
        if caps:
            security["drop_caps"] = list(caps)
        seccomp = sec.get("seccompProfile") if isinstance(sec.get("seccompProfile"), dict) else None
        if seccomp:
            if seccomp.get("type"):
                security["seccomp_type"] = seccomp.get("type")
            if seccomp.get("localhostProfile"):
                security["seccomp_localhost_profile"] = seccomp.get("localhostProfile")
        if not security:
            security = None
    health: dict[str, Any] | None = None
    readiness = probe_from_k8s(c0.get("readinessProbe"), ports_by_name)
    liveness = probe_from_k8s(c0.get("livenessProbe"), ports_by_name)
    startup = probe_from_k8s(c0.get("startupProbe"), ports_by_name)
    if readiness or liveness or startup:
        health = {}
        if readiness:
            health["readiness"] = readiness
        if liveness:
            health["liveness"] = liveness
        if startup:
            health["startup"] = startup

    replicas = int(spec.get("replicas", 1) or 1)
    m_replicas = max(1, replicas)

    app_spec = AppSpec(
        image=image,
        replicas=m_replicas,
        ports=ports,
        command=command or None,
        args=args or None,
        env=env,
        working_dir=working_dir,
        resources=resources,
        security=security,
        health=health,
        pvc_mounts=pvc_mounts,
        volumes=host_mounts,
    )
    if service_spec is not None:
        app_spec = app_spec.model_copy(update={"service": service_spec})
    if ingress_spec is not None:
        app_spec = app_spec.model_copy(update={"ingress": ingress_spec})
    meta_labels = None
    try:
        meta_labels = _metadata(obj).get("labels") or None
    except Exception:  # noqa: S112
        meta_labels = None
    ns = _namespace(obj) or DEFAULT_NAMESPACE
    meta = Metadata(name=_name(obj), namespace=ns, labels=meta_labels)
    return AppManifest(
        apiVersion="ae.dev/v1alpha1", kind="Deployment", metadata=meta, spec=app_spec
    )


def service_spec_from_k8s(
    svc: Any,
    ports_by_name: dict[str, int],
) -> ServiceSpec | None:
    spec = _spec(svc)
    svc_type = spec.get("type", "ClusterIP")
    expose_host = svc_type in {"NodePort", "LoadBalancer"}
    ports_in = spec.get("ports") or []
    if not isinstance(ports_in, Iterable):
        return None
    svc_ports: list[ServiceSpec.ServicePort] = []
    for idx, entry in enumerate(ports_in):
        if not isinstance(entry, dict):
            continue
        try:
            svc_port = int(entry.get("port"))
        except Exception:  # noqa: S112
            continue
        node_port_raw = entry.get("nodePort")
        try:
            node_port = int(node_port_raw) if node_port_raw is not None else None
        except Exception:  # noqa: S112
            node_port = None
        fallback_port = entry.get("port", svc_port)
        tgt_raw = entry.get("targetPort", fallback_port)
        tgt_val = resolve_port_value(tgt_raw, ports_by_name)
        if tgt_val is None:
            tgt_val = fallback_port
        host_port = node_port if node_port is not None and expose_host else None
        svc_ports.append(
            ServiceSpec.ServicePort(
                name=entry.get("name") or f"port-{idx}",
                port=int(host_port or svc_port),
                target_port=tgt_val,
                protocol=entry.get("protocol", "TCP"),
                node_port=node_port if expose_host else None,
            )
        )
    if not svc_ports:
        return None
    return ServiceSpec(
        type=svc_type,
        ports=svc_ports,
        port=svc_ports[0].port if expose_host else None,
        target_port=svc_ports[0].target_port,
        external_ips=spec.get("externalIPs", []),
        session_affinity=spec.get("sessionAffinity"),
    )


def ingress_spec_from_k8s(
    ing: Any, service_name_map: dict[tuple[str | None, str], tuple[str | None, str]]
) -> tuple[tuple[str | None, str], IngressSpec] | None:
    spec = _spec(ing)
    rules = spec.get("rules") or []
    tls_entries = spec.get("tls") or []
    target_key = None
    host = None
    path = "/"
    for rule in rules:
        rule_host = rule.get("host")
        http = rule.get("http") or {}
        for path_entry in http.get("paths", []):
            backend = path_entry.get("backend", {}).get("service", {})
            svc_name = backend.get("name")
            if not svc_name:
                continue
            key = service_name_map.get((_namespace(ing), svc_name))
            if key:
                target_key = key
                host = rule_host
                path = path_entry.get("path") or "/"
                break
        if target_key:
            break
    if not target_key:
        return None
    tls_secret = None
    tls_enabled = False
    if host:
        for entry in tls_entries:
            hosts = entry.get("hosts", []) or []
            if host in hosts:
                tls_enabled = True
                tls_secret = entry.get("secretName")
                break
    ingress_spec = IngressSpec(
        host=host or "",
        path=path,
        tls=tls_enabled,
        tlsSecretName=tls_secret,
    )
    return target_key, ingress_spec
