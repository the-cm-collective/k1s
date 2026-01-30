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


def _service_account_pull_secrets(
    namespace: str, service_account: str, db_path: Path | None = None, dsn: str | None = None
) -> list[str]:
    try:
        from ae.apishim.store import ObjectStore
    except Exception:
        return []
    store = None
    try:
        if dsn or os.getenv("AE_APISHIM_DSN"):
            store = ObjectStore(dsn=dsn or os.getenv("AE_APISHIM_DSN"))
        else:
            db = db_path or Path(os.getenv("AE_APISHIM_DB", "state/apishim.db"))
            if not db.exists():
                return []
            store = ObjectStore(db_path=db)
    except Exception:
        store = None
    if store is None:
        return []
    try:
        sa = store.get("", "v1", "serviceaccounts", namespace, service_account)
    except Exception:
        sa = None
    if sa is None:
        return []
    spec = getattr(sa, "spec", None) or {}
    if not isinstance(spec, dict):
        return []
    secrets = spec.get("imagePullSecrets") or []
    out: list[str] = []
    for entry in secrets:
        if isinstance(entry, dict):
            name = entry.get("name")
            if name:
                out.append(str(name))
        elif entry:
            out.append(str(entry))
    return out


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


def _ports_from_k8s_container(container: dict[str, Any]) -> list[PortSpec]:
    ports: list[PortSpec] = []
    for p in container.get("ports") or []:
        try:
            port_num = int(p.get("containerPort"))
        except Exception:  # noqa: S112
            continue
        name = p.get("name") or f"p{port_num}"
        ports.append(PortSpec(name=name, containerPort=port_num))
    return ports


def _env_from_k8s_container(container: dict[str, Any]) -> list[dict[str, Any]]:
    env: list[dict[str, str]] = []
    for item in container.get("env") or []:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not name:
            continue
        entry: dict[str, Any] = {"name": str(name)}
        value_from = item.get("valueFrom")
        if isinstance(value_from, dict):
            entry["valueFrom"] = value_from
        else:
            entry["value"] = str(item.get("value") or "")
        env.append(entry)
    for ref in container.get("envFrom") or []:
        if not isinstance(ref, dict):
            continue
        prefix = ref.get("prefix")
        cm = ref.get("configMapRef") if isinstance(ref.get("configMapRef"), dict) else None
        sec = ref.get("secretRef") if isinstance(ref.get("secretRef"), dict) else None
        if cm and cm.get("name"):
            entry: dict[str, Any] = {
                "name": "",
                "valueFrom": {"configMapKeyRef": {"name": str(cm["name"]), "key": ""}},
            }
            if prefix:
                entry["valueFrom"]["configMapKeyRef"]["prefix"] = str(prefix)
            env.append(
                entry
            )
        if sec and sec.get("name"):
            entry = {
                "name": "",
                "valueFrom": {"secretKeyRef": {"name": str(sec["name"]), "key": ""}},
            }
            if prefix:
                entry["valueFrom"]["secretKeyRef"]["prefix"] = str(prefix)
            env.append(
                entry
            )
    return env


def _resources_from_k8s_container(container: dict[str, Any]) -> dict[str, Any] | None:
    resources: dict[str, Any] | None = None
    if isinstance(container.get("resources"), dict):
        res = container.get("resources") or {}
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
    return resources


def _security_from_k8s_container(container: dict[str, Any]) -> dict[str, Any] | None:
    security: dict[str, Any] | None = None
    sec = container.get("securityContext") or {}
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
    return security


def _health_from_k8s_container(
    container: dict[str, Any], ports_by_name: dict[str, int]
) -> dict[str, Any] | None:
    health: dict[str, Any] | None = None
    readiness = probe_from_k8s(container.get("readinessProbe"), ports_by_name)
    liveness = probe_from_k8s(container.get("livenessProbe"), ports_by_name)
    startup = probe_from_k8s(container.get("startupProbe"), ports_by_name)
    if readiness or liveness or startup:
        health = {}
        if readiness:
            health["readiness"] = readiness
        if liveness:
            health["liveness"] = liveness
        if startup:
            health["startup"] = startup
    return health


def _container_mounts_from_k8s_container(
    container: dict[str, Any],
    volume_claims: dict[str, tuple[str, bool]],
    volume_hosts: dict[str, str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    pvc_mounts: list[dict[str, Any]] = []
    host_mounts: list[dict[str, Any]] = []
    host_devices: list[dict[str, Any]] = []
    for vm in container.get("volumeMounts") or []:
        if not isinstance(vm, dict):
            continue
        vname = vm.get("name")
        mount_path = vm.get("mountPath")
        if not vname or not mount_path:
            continue
        entry = volume_claims.get(str(vname))
        if entry:
            claim, vol_read_only = entry
            read_only = bool(vm.get("readOnly", False)) or bool(vol_read_only)
            record: dict[str, Any] = {
                "claimName": str(claim),
                "mountPath": str(mount_path),
                "readOnly": read_only,
            }
            sub_path = vm.get("subPath")
            if sub_path:
                record["subPath"] = str(sub_path)
            pvc_mounts.append(record)
        host_path = volume_hosts.get(str(vname))
        if host_path:
            read_only = bool(vm.get("readOnly", False))
            sub_path = vm.get("subPath")
            if sub_path:
                host_path = os.path.join(str(host_path), str(sub_path).lstrip("/"))
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
        device_path = vd.get("devicePath")
        if not vname or not device_path:
            continue
        entry = volume_claims.get(str(vname))
        if entry:
            claim, vol_read_only = entry
            read_only = bool(vol_read_only)
            pvc_mounts.append(
                {
                    "claimName": str(claim),
                    "mountPath": str(device_path),
                    "devicePath": str(device_path),
                    "readOnly": read_only,
                }
            )
        host_path = volume_hosts.get(str(vname))
        if host_path:
            read_only = bool(vd.get("readOnly", False))
            host_devices.append(
                {
                    "hostPath": str(host_path),
                    "devicePath": str(device_path),
                    "readOnly": read_only,
                }
            )
    return pvc_mounts, host_mounts, host_devices


def _container_spec_from_k8s(
    container: dict[str, Any],
    *,
    default_name: str,
    pvc_mounts: list[dict[str, Any]],
    host_mounts: list[dict[str, Any]],
    host_devices: list[dict[str, Any]],
) -> AppSpec.ContainerSpec:
    name = container.get("name") or default_name
    image = container.get("image") or "busybox:latest"
    c_ports = _ports_from_k8s_container(container)
    c_ports_by_name = {
        p.name: int(p.container_port) for p in c_ports if getattr(p, "name", None)
    }
    return AppSpec.ContainerSpec(
        name=str(name),
        image=str(image),
        command=[str(x) for x in (container.get("command") or [])] or None,
        args=[str(x) for x in (container.get("args") or [])] or None,
        env=_env_from_k8s_container(container),
        ports=c_ports,
        resources=_resources_from_k8s_container(container),
        security=_security_from_k8s_container(container),
        working_dir=container.get("workingDir"),
        health=_health_from_k8s_container(container, c_ports_by_name),
        image_pull_policy=container.get("imagePullPolicy"),
        volume_mounts=host_mounts,
        pvc_mounts=pvc_mounts,
        volume_devices=host_devices,
    )


def manifest_from_k8s_workload(
    obj: Any,
    *,
    service_spec: ServiceSpec | None = None,
    ingress_spec: IngressSpec | None = None,
) -> AppManifest:
    spec: dict[str, Any] = _spec(obj)
    tpl = (spec.get("template") or {}).get("spec") or {}
    containers = tpl.get("containers") or []
    init_containers_raw = tpl.get("initContainers") or []
    image_pull_policy = None
    if containers and isinstance(containers[0], dict):
        c0 = containers[0]
    else:
        c0 = {}
    image = c0.get("image") or "busybox:latest"
    image_pull_policy = c0.get("imagePullPolicy")
    ports = _ports_from_k8s_container(c0) if c0 else []
    ports_by_name = {p.name: int(p.container_port) for p in ports if getattr(p, "name", None)}
    command = [str(x) for x in (c0.get("command") or [])]
    args = [str(x) for x in (c0.get("args") or [])]
    env = _env_from_k8s_container(c0)
    working_dir = c0.get("workingDir")
    raw_pull_secrets = tpl.get("imagePullSecrets")
    image_pull_secrets = []
    for secret in (raw_pull_secrets or []):
        if isinstance(secret, dict):
            name = secret.get("name")
            if name:
                image_pull_secrets.append(str(name))
        elif secret:
            image_pull_secrets.append(str(secret))
    pvc_mounts: list[dict[str, Any]] = []
    host_mounts: list[dict[str, Any]] = []
    volume_devices: list[dict[str, Any]] = []
    container_mounts: dict[
        int, tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]
    ] = {}
    init_mounts: dict[
        int, tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]
    ] = {}
    try:
        volume_claims: dict[str, tuple[str, bool]] = {}
        volume_hosts: dict[str, str] = {}
        for vol in tpl.get("volumes") or []:
            if not isinstance(vol, dict):
                continue
            vname = vol.get("name")
            pvc = vol.get("persistentVolumeClaim") or {}
            if isinstance(pvc, dict):
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
            for idx, container in enumerate(containers or []):
                if not isinstance(container, dict):
                    continue
                c_pvc, c_host, c_devs = _container_mounts_from_k8s_container(
                    container, volume_claims, volume_hosts
                )
                container_mounts[idx] = (c_pvc, c_host, c_devs)
            for idx, container in enumerate(init_containers_raw or []):
                if not isinstance(container, dict):
                    continue
                c_pvc, c_host, c_devs = _container_mounts_from_k8s_container(
                    container, volume_claims, volume_hosts
                )
                init_mounts[idx] = (c_pvc, c_host, c_devs)
    except Exception:  # noqa: S112 - best-effort PVC extraction
        pvc_mounts = []
        host_mounts = []
        volume_devices = []
        container_mounts = {}
        init_mounts = {}
    main_pvc, main_host, main_devs = container_mounts.get(0, ([], [], []))
    pvc_mounts = list(main_pvc)
    host_mounts = list(main_host)
    volume_devices = list(main_devs)
    resources = _resources_from_k8s_container(c0)
    security = _security_from_k8s_container(c0)
    health = _health_from_k8s_container(c0, ports_by_name)

    replicas = int(spec.get("replicas", 1) or 1)
    m_replicas = max(1, replicas)

    service_account = tpl.get("serviceAccountName") or tpl.get("serviceAccount") or "default"
    namespace = _namespace(obj) or DEFAULT_NAMESPACE
    sidecars: list[AppSpec.ContainerSpec] = []
    if len(containers) > 1:
        for idx, container in enumerate(containers[1:], start=1):
            if not isinstance(container, dict):
                continue
            c_pvc, c_host, c_devs = container_mounts.get(idx, ([], [], []))
            sidecars.append(
                _container_spec_from_k8s(
                    container,
                    default_name=f"sidecar-{idx}",
                    pvc_mounts=c_pvc,
                    host_mounts=c_host,
                    host_devices=c_devs,
                )
            )
    init_containers: list[AppSpec.ContainerSpec] = []
    if init_containers_raw:
        for idx, container in enumerate(init_containers_raw):
            if not isinstance(container, dict):
                continue
            c_pvc, c_host, c_devs = init_mounts.get(idx, ([], [], []))
            init_containers.append(
                _container_spec_from_k8s(
                    container,
                    default_name=f"init-{idx}",
                    pvc_mounts=c_pvc,
                    host_mounts=c_host,
                    host_devices=c_devs,
                )
            )

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
        volume_devices=volume_devices,
        service_account_name=service_account,
        image_pull_secrets=image_pull_secrets,
        image_pull_policy=image_pull_policy,
        containers=sidecars,
        init_containers=init_containers,
    )
    if raw_pull_secrets is None and not image_pull_secrets:
        sa_pull = _service_account_pull_secrets(namespace, service_account)
        if sa_pull:
            image_pull_secrets = sa_pull
            app_spec = app_spec.model_copy(update={"image_pull_secrets": image_pull_secrets})
    if len(image_pull_secrets) == 1:
        app_spec = app_spec.model_copy(update={"registry_auth_ref": image_pull_secrets[0]})
    if service_spec is not None:
        app_spec = app_spec.model_copy(update={"service": service_spec})
    if ingress_spec is not None:
        app_spec = app_spec.model_copy(update={"ingress": ingress_spec})
    meta_labels = None
    try:
        meta_labels = _metadata(obj).get("labels") or None
    except Exception:  # noqa: S112
        meta_labels = None
    meta = Metadata(name=_name(obj), namespace=namespace, labels=meta_labels)
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
