"""Edge-local ingress renderer (Caddyfile) driven by route bundles."""

from __future__ import annotations

import hashlib
import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

LOGGER = logging.getLogger(__name__)
_UPSTREAM_MODES = {"auto", "bundle-endpoints", "dns"}


@dataclass(frozen=True)
class EdgeLocalIngressConfig:
    config_dir: Path
    config_file: Path
    reload_cmd: str | None
    service_domain: str | None
    service_port_fallback: int
    upstream_mode: str = "auto"


class EdgeLocalIngressRenderer:
    def __init__(self, config: EdgeLocalIngressConfig) -> None:
        self._config = config
        self._last_hash: str | None = None

    def apply_bundle(self, bundle: dict) -> tuple[bool, str | None]:
        try:
            routes = bundle.get("routes") if isinstance(bundle.get("routes"), list) else []
            policies = (
                bundle.get("policies") if isinstance(bundle.get("policies"), list) else []
            )
            service_endpoints = (
                bundle.get("service_endpoints")
                if isinstance(bundle.get("service_endpoints"), dict)
                else {}
            )
            content = render_edge_local_caddy(
                routes, policies, self._config, service_endpoints=service_endpoints
            )
        except Exception as exc:  # noqa: BLE001
            return False, f"render_failed:{exc}"
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if self._last_hash == digest:
            return True, None
        try:
            self._config.config_dir.mkdir(parents=True, exist_ok=True)
            self._config.config_file.write_text(content, encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            return False, f"write_failed:{exc}"
        if self._config.reload_cmd:
            try:
                subprocess.run(self._config.reload_cmd, shell=True, check=False)  # noqa: S602
            except Exception as exc:  # noqa: BLE001
                return False, f"reload_failed:{exc}"
        self._last_hash = digest
        return True, None


def build_edge_local_renderer() -> EdgeLocalIngressRenderer | None:
    raw_dir = os.getenv("AE_EDGE_LOCAL_INGRESS_CONFIG_DIR")
    if not raw_dir:
        return None
    config_dir = Path(raw_dir)
    config_file = Path(
        os.getenv("AE_EDGE_LOCAL_INGRESS_CONFIG_FILE", config_dir / "edge-local.caddy")
    )
    reload_cmd = os.getenv("AE_EDGE_LOCAL_INGRESS_RELOAD_CMD")
    service_domain = os.getenv("AE_EDGE_LOCAL_SERVICE_DOMAIN", "") or None
    try:
        port_fallback = int(os.getenv("AE_EDGE_LOCAL_SERVICE_PORT_FALLBACK", "80") or 80)
    except Exception:
        port_fallback = 80
    upstream_mode = str(os.getenv("AE_EDGE_LOCAL_UPSTREAM_MODE", "auto")).strip().lower()
    if upstream_mode not in _UPSTREAM_MODES:
        LOGGER.warning(
            "edge-local invalid AE_EDGE_LOCAL_UPSTREAM_MODE=%s (expected one of %s); using auto",
            upstream_mode,
            sorted(_UPSTREAM_MODES),
        )
        upstream_mode = "auto"
    return EdgeLocalIngressRenderer(
        EdgeLocalIngressConfig(
            config_dir=config_dir,
            config_file=config_file,
            reload_cmd=reload_cmd,
            service_domain=service_domain,
            service_port_fallback=port_fallback,
            upstream_mode=upstream_mode,
        )
    )


def render_edge_local_caddy(
    routes: Iterable[dict],
    policies: Iterable[dict],
    config: EdgeLocalIngressConfig,
    service_endpoints: dict[str, list[dict[str, Any]]] | None = None,
) -> str:
    service_endpoints = service_endpoints if isinstance(service_endpoints, dict) else {}
    policy_map = {}
    for policy_doc in policies:
        if not isinstance(policy_doc, dict):
            continue
        meta = policy_doc.get("metadata") if isinstance(policy_doc.get("metadata"), dict) else {}
        name = str(meta.get("name") or "").strip()
        namespace = str(meta.get("namespace") or "default").strip() or "default"
        if not name:
            continue
        spec = policy_doc.get("spec") if isinstance(policy_doc.get("spec"), dict) else {}
        policy_map[(name, namespace)] = spec

    host_map: dict[str, list[dict]] = {}
    for doc in routes:
        if not isinstance(doc, dict):
            continue
        spec = doc.get("spec") if isinstance(doc.get("spec"), dict) else {}
        host = str(spec.get("host") or "").strip()
        if not host:
            continue
        meta = doc.get("metadata") if isinstance(doc.get("metadata"), dict) else {}
        namespace = str(meta.get("namespace") or "default").strip() or "default"
        policy_ref = spec.get("policyRef") if isinstance(spec.get("policyRef"), dict) else {}
        policy_name = str(policy_ref.get("name") or "").strip()
        policy_ns = str(policy_ref.get("namespace") or namespace).strip() or namespace
        policy_spec = policy_map.get((policy_name, policy_ns)) if policy_name else None

        paths = spec.get("paths") if isinstance(spec.get("paths"), list) else []
        if not paths:
            service_ref = spec.get("serviceRef") if isinstance(spec.get("serviceRef"), dict) else {}
            upstreams = _upstreams_for_service(
                service_ref, namespace, config, service_endpoints
            )
            if upstreams:
                host_map.setdefault(host, []).append(
                    {
                        "path": "/",
                        "upstreams": upstreams,
                        "policy": policy_spec,
                    }
                )
            continue
        for entry in paths:
            if not isinstance(entry, dict):
                continue
            path = str(entry.get("path") or "/").strip() or "/"
            if not path.startswith("/"):
                path = f"/{path}"
            service_ref = (
                entry.get("serviceRef") if isinstance(entry.get("serviceRef"), dict) else {}
            )
            upstreams = _upstreams_for_service(
                service_ref, namespace, config, service_endpoints
            )
            if not upstreams:
                continue
            host_map.setdefault(host, []).append(
                {
                    "path": path,
                    "upstreams": upstreams,
                    "policy": policy_spec,
                }
            )

    blocks = []
    for host, entries in sorted(host_map.items()):
        blocks.append(_render_site_block(host, entries))
    if not blocks:
        # Keep output syntactically valid for Caddy even when there are no
        # routable edge-local hosts yet, without capturing real traffic.
        blocks.append("https://edge-local-unconfigured.invalid {\n    respond 503\n}\n")
    return "\n\n".join(blocks) + "\n"


def _render_site_block(host: str, entries: list[dict]) -> str:
    lines = [f"https://{host} {{"]
    lines.append("    log {")
    lines.append("        output stdout")
    lines.append("        format console")
    lines.append("    }")
    lines.append("    header -Strict-Transport-Security")
    lines.append("    tls internal")
    lines.append("    route {")

    def _path_key(item: dict) -> int:
        return len(item.get("path") or "")

    for entry in sorted(entries, key=_path_key, reverse=True):
        path = entry.get("path") or "/"
        upstreams = entry.get("upstreams")
        policy = entry.get("policy") if isinstance(entry.get("policy"), dict) else None
        if not isinstance(upstreams, list) or not upstreams:
            continue
        block_lines = _render_route_block(path, upstreams, policy)
        lines.extend(["        " + line for line in block_lines])

    lines.append("    }")
    lines.append("}")
    return "\n".join(lines)


def _render_route_block(path: str, upstreams: list[str], policy: dict | None) -> list[str]:
    is_root = path == "/"
    prefix = "handle" if is_root else f"handle_path {path}*"
    lines = [f"{prefix} {{"]

    if policy:
        lines.extend(_render_ip_filters(policy, indent=1))
        max_body = _extract_max_body_bytes(policy)
        if max_body:
            lines.append("    request_body {")
            lines.append(f"        max_size {int(max_body)}")
            lines.append("    }")
        response_headers = _header_kv(policy, "response")
        if response_headers:
            lines.append("    header {")
            for key, value in response_headers:
                if value is None:
                    lines.append(f"        -{key}")
                else:
                    lines.append(f"        +{key} {value}")
            lines.append("    }")

    lines.append(f"    reverse_proxy {' '.join(upstreams)} {{")
    if policy:
        request_headers = _header_kv(policy, "request")
        for key, value in request_headers:
            if value is None:
                lines.append(f"        header_up -{key}")
            else:
                lines.append(f"        header_up {key} {value}")
        timeout_block = _render_timeouts(policy)
        if timeout_block:
            lines.extend(["        " + line for line in timeout_block])
    lines.append("    }")
    lines.append("}")
    return lines


def _render_ip_filters(policy: dict, indent: int = 0) -> list[str]:
    lines: list[str] = []
    waf = policy.get("waf") if isinstance(policy.get("waf"), dict) else {}
    basic = waf.get("basic") if isinstance(waf.get("basic"), dict) else {}
    allow = basic.get("ipAllowlist") if isinstance(basic.get("ipAllowlist"), list) else []
    deny = basic.get("ipDenylist") if isinstance(basic.get("ipDenylist"), list) else []
    pad = "    " * indent
    if deny:
        lines.append(f"{pad}@deny remote_ip {' '.join(deny)}")
        lines.append(f"{pad}respond @deny 403")
    if allow:
        lines.append(f"{pad}@notallow not remote_ip {' '.join(allow)}")
        lines.append(f"{pad}respond @notallow 403")
    return lines


def _render_timeouts(policy: dict) -> list[str]:
    timeouts = policy.get("timeouts") if isinstance(policy.get("timeouts"), dict) else {}
    if not timeouts:
        return []
    read_ms = _coerce_int(timeouts.get("requestHeadersMs"))
    write_ms = _coerce_int(timeouts.get("requestBodyMs"))
    idle_ms = _coerce_int(timeouts.get("idleMs"))
    if not any([read_ms, write_ms, idle_ms]):
        return []
    lines = ["transport http {"]
    if read_ms:
        lines.append(f"    read_timeout {read_ms/1000:.3f}s")
    if write_ms:
        lines.append(f"    write_timeout {write_ms/1000:.3f}s")
    if idle_ms:
        lines.append(f"    idle_timeout {idle_ms/1000:.3f}s")
    lines.append("}")
    return lines


def _extract_max_body_bytes(policy: dict) -> int | None:
    if "maxBodyBytes" in policy:
        return _coerce_int(policy.get("maxBodyBytes"))
    waf = policy.get("waf") if isinstance(policy.get("waf"), dict) else {}
    basic = waf.get("basic") if isinstance(waf.get("basic"), dict) else {}
    return _coerce_int(basic.get("maxBodyBytes"))


def _header_kv(policy: dict, section: str) -> list[tuple[str, str | None]]:
    headers = policy.get("headers") if isinstance(policy.get("headers"), dict) else {}
    target = headers.get(section) if isinstance(headers.get(section), dict) else {}
    out: list[tuple[str, str | None]] = []
    add = target.get("add") if isinstance(target.get("add"), dict) else {}
    for key, value in add.items():
        if key:
            out.append((str(key), str(value)))
    remove = target.get("remove") if isinstance(target.get("remove"), list) else []
    for key in remove:
        if key:
            out.append((str(key), None))
    return out


def _upstreams_for_service(
    service_ref: dict,
    namespace: str,
    config: EdgeLocalIngressConfig,
    service_endpoints: dict[str, list[dict[str, Any]]],
) -> list[str]:
    service_name = str(service_ref.get("name") or "").strip()
    if not service_name:
        return []
    service_ns = str(service_ref.get("namespace") or namespace).strip() or namespace
    service_key = f"{service_ns}/{service_name}"
    port_hint = _coerce_int(service_ref.get("port"))

    mode = config.upstream_mode if config.upstream_mode in _UPSTREAM_MODES else "auto"
    if mode in {"auto", "bundle-endpoints"}:
        bundle_upstreams = _bundle_upstreams_for_service(
            service_endpoints, service_key, port_hint
        )
        if bundle_upstreams:
            return bundle_upstreams
        if mode == "bundle-endpoints":
            LOGGER.warning(
                "edge-local route skipped: missing bundle endpoints service=%s port_hint=%s",
                service_key,
                port_hint,
            )
            return []
        LOGGER.debug(
            "edge-local falling back to DNS upstream service=%s port_hint=%s",
            service_key,
            port_hint,
        )

    fallback = _dns_upstream_for_service(service_ref, namespace, config)
    return [fallback] if fallback else []


def _bundle_upstreams_for_service(
    service_endpoints: dict[str, list[dict[str, Any]]],
    service_key: str,
    port_hint: int | None,
) -> list[str]:
    entries = service_endpoints.get(service_key)
    if not isinstance(entries, list):
        return []

    matched: list[str] = []
    all_candidates: list[str] = []
    for item in entries:
        if not isinstance(item, dict):
            continue
        ready = item.get("ready")
        if ready is False:
            continue
        ip = str(item.get("ip") or "").strip()
        target_port = _coerce_int(item.get("target_port"))
        service_port = _coerce_int(item.get("service_port") or item.get("port"))
        if not ip or target_port is None:
            continue
        upstream = f"{ip}:{target_port}"
        all_candidates.append(upstream)
        if port_hint is not None and (
            (service_port is not None and service_port == port_hint) or target_port == port_hint
        ):
            matched.append(upstream)

    if port_hint is not None and matched:
        return _dedupe_preserving_order(matched)
    return _dedupe_preserving_order(all_candidates)


def _dns_upstream_for_service(
    service_ref: dict, namespace: str, config: EdgeLocalIngressConfig
) -> str | None:
    name = str(service_ref.get("name") or "").strip()
    if not name:
        return None
    svc_ns = str(service_ref.get("namespace") or namespace).strip() or namespace
    port = _coerce_int(service_ref.get("port")) or config.service_port_fallback
    host = name
    if svc_ns:
        host = f"{host}.{svc_ns}"
    if config.service_domain:
        suffix = str(config.service_domain).strip().lstrip(".")
        if suffix:
            host = f"{host}.{suffix}"
    return f"{host}:{port}"


def _dedupe_preserving_order(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def _coerce_int(value) -> int | None:
    try:
        if value is None:
            return None
        return int(value)
    except Exception:
        return None


__all__ = [
    "EdgeLocalIngressConfig",
    "EdgeLocalIngressRenderer",
    "build_edge_local_renderer",
    "render_edge_local_caddy",
]
