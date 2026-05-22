#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="microk8s_stack_bundle.py",
        description="Render a Host A bootstrap bundle for the MicroK8s k1s dev stack.",
    )
    parser.add_argument("--release", required=True, help="Helm release name for k1s-core-ha")
    parser.add_argument("--namespace", required=True, help="Namespace containing the core release")
    parser.add_argument("--site-id", default="host-a", help="Remote site identifier")
    parser.add_argument(
        "--from-kube",
        action="store_true",
        help="Read bootstrap data from the live cluster via kubectl",
    )
    parser.add_argument(
        "--kubectl",
        default=os.getenv("KUBECTL_BIN", "kubectl"),
        help="kubectl binary to use with --from-kube",
    )
    parser.add_argument("--configmap-name", default="", help="Explicit bootstrap ConfigMap name")
    parser.add_argument("--auth-secret-name", default="", help="Explicit auth Secret name")
    parser.add_argument("--stack-domain", default="", help="Stack domain override")
    parser.add_argument("--wildcard-apps-domain", default="", help="Wildcard apps domain override")
    parser.add_argument("--registry-host", default="", help="Registry host override")
    parser.add_argument("--controller-host", default="", help="Controller endpoint host override")
    parser.add_argument("--controller-port", type=int, default=9110, help="Controller endpoint port")
    parser.add_argument("--nats-leaf-host", default="", help="NATS leaf endpoint host override")
    parser.add_argument("--nats-leaf-port", type=int, default=7422, help="NATS leaf endpoint port")
    parser.add_argument("--nats-leaf-user", default="", help="NATS leaf user override")
    parser.add_argument("--nats-leaf-password", default="", help="NATS leaf password override")
    parser.add_argument("--rathole-host", default="", help="Rathole endpoint host override")
    parser.add_argument("--rathole-port", type=int, default=2333, help="Rathole endpoint port")
    parser.add_argument("--agent-token", default="", help="Agent token override")
    parser.add_argument("--rathole-token", default="", help="Rathole token override")
    parser.add_argument(
        "--format",
        choices=("json", "env"),
        default="json",
        help="Output format",
    )
    parser.add_argument(
        "--output",
        default="",
        help="Write output to this path instead of stdout",
    )
    return parser


def kubectl_get(
    kubectl_bin: str,
    namespace: str,
    kind: str,
    name: str,
) -> dict[str, Any]:
    cmd = [kubectl_bin, "-n", namespace, "get", kind, name, "-o", "json"]
    proc = subprocess.run(cmd, check=False, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"kubectl failed for {kind}/{name}: {proc.stderr.strip() or proc.stdout.strip()}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:  # pragma: no cover - defensive
        raise RuntimeError(f"kubectl returned invalid JSON for {kind}/{name}") from exc


def decode_secret_value(secret_obj: dict[str, Any], key: str) -> str:
    payload = secret_obj.get("data") or {}
    value = payload.get(key)
    if not value:
        return ""
    return base64.b64decode(str(value).encode("utf-8")).decode("utf-8")


def service_host(service_obj: dict[str, Any], fallback_dns: str) -> str:
    ingress = (((service_obj.get("status") or {}).get("loadBalancer") or {}).get("ingress") or [])
    if ingress:
        first = ingress[0] or {}
        ip = str(first.get("ip") or "").strip()
        hostname = str(first.get("hostname") or "").strip()
        if ip:
            return ip
        if hostname:
            return hostname
    spec = service_obj.get("spec") or {}
    cluster_ip = str(spec.get("clusterIP") or "").strip()
    if cluster_ip and cluster_ip.lower() != "none":
        return cluster_ip
    return fallback_dns


def default_fullname(release: str) -> str:
    return f"{release}-k1s-core-ha"


def default_configmap_name(release: str) -> str:
    return f"{default_fullname(release)}-bootstrap"


def default_auth_secret_name(release: str) -> str:
    return f"{default_fullname(release)}-auth"


def parse_host_port(value: str, default_port: int) -> tuple[str, int]:
    raw = str(value or "").strip()
    if not raw:
        return "", default_port
    if "://" not in raw:
        raw = f"tcp://{raw}"
    parsed = urlparse(raw)
    return parsed.hostname or "", int(parsed.port or default_port)


def choose_value(*candidates: str) -> str:
    for item in candidates:
        value = str(item or "").strip()
        if value:
            return value
    return ""


def load_cluster_bundle(args: argparse.Namespace) -> dict[str, Any]:
    configmap_name = args.configmap_name or default_configmap_name(args.release)
    configmap = kubectl_get(args.kubectl, args.namespace, "configmap", configmap_name)
    data = configmap.get("data") or {}

    auth_secret_name = args.auth_secret_name or str(data.get("auth_secret_name") or "").strip()
    if not auth_secret_name:
        auth_secret_name = default_auth_secret_name(args.release)
    secret = kubectl_get(args.kubectl, args.namespace, "secret", auth_secret_name)

    controller_service_name = choose_value(
        str(data.get("controller_external_service") or ""),
        f"{default_fullname(args.release)}-controller-external",
    )
    controller_service = kubectl_get(args.kubectl, args.namespace, "service", controller_service_name)
    controller_dns = f"{controller_service_name}.{args.namespace}.svc.cluster.local"
    controller_host = choose_value(
        args.controller_host,
        str(data.get("controller_external_host_hint") or ""),
        service_host(controller_service, controller_dns),
    )

    nats_service_name = choose_value(
        str(data.get("nats_leaf_external_service") or ""),
        f"{default_fullname(args.release)}-nats-leaf",
    )
    nats_service = kubectl_get(args.kubectl, args.namespace, "service", nats_service_name)
    nats_dns = f"{nats_service_name}.{args.namespace}.svc.cluster.local"
    nats_host = choose_value(
        args.nats_leaf_host,
        str(data.get("nats_leaf_host_hint") or ""),
        service_host(nats_service, nats_dns),
    )

    rathole_service_name = choose_value(
        str(data.get("rathole_external_service") or ""),
        f"{default_fullname(args.release)}-rathole",
    )
    rathole_service = kubectl_get(args.kubectl, args.namespace, "service", rathole_service_name)
    rathole_dns = f"{rathole_service_name}.{args.namespace}.svc.cluster.local"
    rathole_host = choose_value(
        args.rathole_host,
        str(data.get("rathole_external_host_hint") or ""),
        service_host(rathole_service, rathole_dns),
    )

    return {
        "stack_domain": choose_value(args.stack_domain, str(data.get("stack_domain") or "")),
        "wildcard_apps_domain": choose_value(
            args.wildcard_apps_domain,
            str(data.get("wildcard_apps_domain") or ""),
        ),
        "registry_host": choose_value(args.registry_host, str(data.get("registry_host") or "")),
        "controller_host": controller_host,
        "controller_port": int(str(data.get("controller_external_port") or args.controller_port) or args.controller_port),
        "nats_leaf_host": nats_host,
        "nats_leaf_port": int(str(data.get("nats_leaf_port") or args.nats_leaf_port) or args.nats_leaf_port),
        "rathole_host": rathole_host,
        "rathole_port": int(str(data.get("rathole_port") or args.rathole_port) or args.rathole_port),
        "docs_url": str(data.get("docs_url") or "").strip(),
        "dash_url": str(data.get("dash_url") or "").strip(),
        "agent_token": choose_value(args.agent_token, decode_secret_value(secret, "agent-token")),
        "rathole_token": choose_value(args.rathole_token, decode_secret_value(secret, "rathole-token")),
        "nats_leaf_user": choose_value(args.nats_leaf_user, decode_secret_value(secret, "nats-leaf-user")),
        "nats_leaf_password": choose_value(
            args.nats_leaf_password,
            decode_secret_value(secret, "nats-leaf-password"),
        ),
    }


def load_manual_bundle(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "stack_domain": args.stack_domain,
        "wildcard_apps_domain": args.wildcard_apps_domain,
        "registry_host": args.registry_host,
        "controller_host": args.controller_host,
        "controller_port": args.controller_port,
        "nats_leaf_host": args.nats_leaf_host,
        "nats_leaf_port": args.nats_leaf_port,
        "rathole_host": args.rathole_host,
        "rathole_port": args.rathole_port,
        "docs_url": "",
        "dash_url": "",
        "agent_token": args.agent_token,
        "rathole_token": args.rathole_token,
        "nats_leaf_user": args.nats_leaf_user,
        "nats_leaf_password": args.nats_leaf_password,
    }


def build_bundle(args: argparse.Namespace) -> dict[str, Any]:
    raw = load_cluster_bundle(args) if args.from_kube else load_manual_bundle(args)

    required = {
        "stack_domain": raw.get("stack_domain"),
        "controller_host": raw.get("controller_host"),
        "agent_token": raw.get("agent_token"),
        "nats_leaf_host": raw.get("nats_leaf_host"),
        "rathole_host": raw.get("rathole_host"),
    }
    missing = sorted(key for key, value in required.items() if not str(value or "").strip())
    if missing:
        raise RuntimeError(
            "missing required bundle fields: "
            + ", ".join(missing)
            + " (set them explicitly or use --from-kube)"
        )

    controller_url = f"http://{raw['controller_host']}:{int(raw['controller_port'])}"
    nats_leaf_addr = f"{raw['nats_leaf_host']}:{int(raw['nats_leaf_port'])}"
    rathole_server_addr = f"{raw['rathole_host']}:{int(raw['rathole_port'])}"

    nats_leaf_url = ""
    if str(raw.get("nats_leaf_user") or "").strip() and str(raw.get("nats_leaf_password") or "").strip():
        nats_leaf_url = (
            f"nats://{raw['nats_leaf_user']}:{raw['nats_leaf_password']}"
            f"@{raw['nats_leaf_host']}:{int(raw['nats_leaf_port'])}"
        )

    edge_env: dict[str, str] = {
        "EDGE_PROFILE": "k1s-ha-core",
        "EDGE_INGRESS_MODE": "core-proxy",
        "AE_RUNTIME_BACKEND": "cri",
        "AE_INFRA_BACKEND": "cri",
        "AE_CONTROLLER_URL": controller_url,
        "AE_AGENT_TOKEN": str(raw["agent_token"]),
        "AE_SITE_ID": args.site_id,
        "AE_RATHOLE_SERVER_ADDR": rathole_server_addr,
        "AE_RATHOLE_DEFAULT_TOKEN": str(raw.get("rathole_token") or ""),
        "AE_REGISTRY_HOST": str(raw.get("registry_host") or ""),
        "K1S_STACK_DOMAIN": str(raw["stack_domain"]),
        "K1S_WILDCARD_APPS_DOMAIN": str(raw.get("wildcard_apps_domain") or ""),
        "K1S_NATS_LEAF_ADDR": nats_leaf_addr,
    }
    nats_host, nats_port = parse_host_port(nats_leaf_addr, int(raw["nats_leaf_port"]))
    if nats_host:
        edge_env["AE_NATS_HUB_LEAF_HOST"] = nats_host
        edge_env["AE_NATS_HUB_LEAF_PORT"] = str(nats_port)
    if nats_leaf_url:
        edge_env["K1S_NATS_LEAF_URL"] = nats_leaf_url

    bundle = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "release": args.release,
        "namespace": args.namespace,
        "site_id": args.site_id,
        "stack_domain": raw["stack_domain"],
        "wildcard_apps_domain": raw.get("wildcard_apps_domain") or "",
        "registry_host": raw.get("registry_host") or "",
        "controller_url": controller_url,
        "agent_token": raw["agent_token"],
        "nats_leaf_addr": nats_leaf_addr,
        "nats_leaf_url": nats_leaf_url,
        "rathole_server_addr": rathole_server_addr,
        "rathole_token": raw.get("rathole_token") or "",
        "dash_url": raw.get("dash_url") or "",
        "docs_url": raw.get("docs_url") or "",
        "suggested_edge_env": edge_env,
    }
    return bundle


def render_env(bundle: dict[str, Any]) -> str:
    env_map = dict(bundle.get("suggested_edge_env") or {})
    env_map["K1S_CONTROLLER_URL"] = str(bundle["controller_url"])
    env_map["K1S_AGENT_TOKEN"] = str(bundle["agent_token"])
    env_map["K1S_RATHOLE_SERVER_ADDR"] = str(bundle["rathole_server_addr"])
    env_map["K1S_RATHOLE_TOKEN"] = str(bundle.get("rathole_token") or "")
    ordered_keys = [
        "EDGE_PROFILE",
        "EDGE_INGRESS_MODE",
        "AE_RUNTIME_BACKEND",
        "AE_INFRA_BACKEND",
        "AE_SITE_ID",
        "AE_CONTROLLER_URL",
        "AE_AGENT_TOKEN",
        "AE_NATS_HUB_LEAF_HOST",
        "AE_NATS_HUB_LEAF_PORT",
        "AE_RATHOLE_SERVER_ADDR",
        "AE_RATHOLE_DEFAULT_TOKEN",
        "AE_REGISTRY_HOST",
        "K1S_STACK_DOMAIN",
        "K1S_WILDCARD_APPS_DOMAIN",
        "K1S_NATS_LEAF_ADDR",
        "K1S_NATS_LEAF_URL",
        "K1S_CONTROLLER_URL",
        "K1S_AGENT_TOKEN",
        "K1S_RATHOLE_SERVER_ADDR",
        "K1S_RATHOLE_TOKEN",
    ]
    lines: list[str] = []
    for key in ordered_keys:
        value = str(env_map.get(key) or "")
        if value:
            lines.append(f"{key}={value}")
    return "\n".join(lines) + "\n"


def write_output(payload: str, output_path: str) -> None:
    if not output_path:
        sys.stdout.write(payload)
        return
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        bundle = build_bundle(args)
    except Exception as exc:  # noqa: BLE001
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.format == "env":
        write_output(render_env(bundle), args.output)
        return 0

    payload = json.dumps(bundle, indent=2, sort_keys=True) + "\n"
    write_output(payload, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
