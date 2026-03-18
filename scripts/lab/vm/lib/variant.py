#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_SMOKE_LANES = [
    "single_non_gpu",
    "single_gpu",
    "multi_non_gpu",
    "multi_gpu",
]
SUPPORTED_SMOKE_LANES = [*DEFAULT_SMOKE_LANES, "ha_control_plane"]
DEFAULT_PHASE_TIMEOUTS = {
    "provision": 1800,
    "bootstrap": 1800,
    "service_ready": 900,
    "fabric_validate": 600,
    "functional_basic": 300,
    "ha_acceptance": 900,
}
DEFAULT_RETRY_POLICY = {
    "initial_backoff_s": 2.0,
    "max_backoff_s": 15.0,
    "jitter_s": 1.0,
}
DEFAULT_SMOKE_CHECKS = {
    "service_ready": True,
    "fabric_validate": True,
    "functional_basic": True,
    "functional_advanced": False,
    "ha_acceptance": True,
}
ALLOWED_HOST_ROLES = {"k1s-core", "k1s-ha-core", "k1s-edge-core", "k1s-edge-node"}
ALLOWED_HA_SCHEMES = {"http", "https"}


def _must(obj: dict[str, Any], key: str, t: type, path: str) -> Any:
    if key not in obj:
        raise ValueError(f"missing {path}.{key}")
    value = obj[key]
    if not isinstance(value, t):
        raise ValueError(f"{path}.{key} must be {t.__name__}")
    return value


def _resolve_repo_path(raw_path: str) -> str:
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = REPO_ROOT / candidate
    return str(candidate.resolve())


def _normalize_host(host: dict[str, Any], idx: int) -> dict[str, Any]:
    name = _must(host, "name", str, f"hosts[{idx}]")
    ip = _must(host, "ip", str, f"hosts[{idx}]")
    role = _must(host, "role", str, f"hosts[{idx}]")
    if role not in ALLOWED_HOST_ROLES:
        raise ValueError(
            f"hosts[{idx}].role must be one of k1s-core|k1s-ha-core|k1s-edge-core|k1s-edge-node"
        )
    return {
        "name": name,
        "ip": ip,
        "role": role,
        "gpu": bool(host.get("gpu", False)),
        "site_id": str(host.get("site_id", "")).strip() or None,
        "node_id": str(host.get("node_id", "")).strip() or name,
        "node_labels": str(host.get("node_labels", "")).strip() or None,
        "agent_port": int(
            host.get("agent_port", 9112 if role not in {"k1s-core", "k1s-ha-core"} else 9111)
        ),
    }


def _csv_list(raw: Any, path: str) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        return [part.strip() for part in raw.split(",") if part.strip()]
    if isinstance(raw, list):
        items: list[str] = []
        for idx, value in enumerate(raw):
            if not isinstance(value, str):
                raise ValueError(f"{path}[{idx}] must be a string")
            clean = value.strip()
            if not clean:
                raise ValueError(f"{path}[{idx}] must not be empty")
            items.append(clean)
        return items
    raise ValueError(f"{path} must be a string or list")


def _parse_ha(raw: dict[str, Any], hosts: list[dict[str, Any]]) -> dict[str, Any]:
    if raw and not isinstance(raw, dict):
        raise ValueError("ha must be a mapping")
    use_raw = raw if isinstance(raw, dict) else {}
    has_ha_hosts = any(host["role"] == "k1s-ha-core" for host in hosts)
    if has_ha_hosts and not use_raw:
        raise ValueError("ha mapping required when hosts include role=k1s-ha-core")

    etcd_endpoints = _csv_list(use_raw.get("etcd_endpoints"), "ha.etcd_endpoints")
    etcd_prefix = str(use_raw.get("etcd_prefix", "")).strip()
    nats_url = str(use_raw.get("nats_url", "")).strip()
    hub_nodes_raw = use_raw.get("hub_nodes") or []
    edge_sites_raw = use_raw.get("edge_sites") or []
    drills_raw = use_raw.get("drills") or {}
    controller_scheme = str(use_raw.get("controller_scheme", "http")).strip().lower()
    apishim_scheme = str(use_raw.get("apishim_scheme", "http")).strip().lower()
    controller_metrics_url = str(use_raw.get("controller_metrics_url", "")).strip() or None

    if controller_scheme not in ALLOWED_HA_SCHEMES:
        raise ValueError("ha.controller_scheme must be http or https")
    if apishim_scheme not in ALLOWED_HA_SCHEMES:
        raise ValueError("ha.apishim_scheme must be http or https")

    if has_ha_hosts:
        if not etcd_endpoints:
            raise ValueError("ha.etcd_endpoints required when hosts include role=k1s-ha-core")
        if not etcd_prefix:
            raise ValueError("ha.etcd_prefix required when hosts include role=k1s-ha-core")
        if not nats_url:
            raise ValueError("ha.nats_url required when hosts include role=k1s-ha-core")

    hub_nodes: list[dict[str, str]] = []
    if not isinstance(hub_nodes_raw, list):
        raise ValueError("ha.hub_nodes must be a list")
    for idx, entry in enumerate(hub_nodes_raw):
        if not isinstance(entry, dict):
            raise ValueError(f"ha.hub_nodes[{idx}] must be a mapping")
        name = str(entry.get("name", "")).strip()
        monitor_url = str(entry.get("monitor_url", "")).strip()
        if not name or not monitor_url:
            raise ValueError(f"ha.hub_nodes[{idx}] requires name and monitor_url")
        hub_nodes.append({"name": name, "monitor_url": monitor_url})

    edge_sites: list[dict[str, Any]] = []
    if not isinstance(edge_sites_raw, list):
        raise ValueError("ha.edge_sites must be a list")
    for idx, entry in enumerate(edge_sites_raw):
        if not isinstance(entry, dict):
            raise ValueError(f"ha.edge_sites[{idx}] must be a mapping")
        site_id = str(entry.get("site_id", "")).strip()
        monitor_url = str(entry.get("monitor_url", "")).strip()
        expected_gateways = _csv_list(
            entry.get("expected_gateways"),
            f"ha.edge_sites[{idx}].expected_gateways",
        )
        if not site_id or not monitor_url:
            raise ValueError(f"ha.edge_sites[{idx}] requires site_id and monitor_url")
        edge_sites.append(
            {
                "site_id": site_id,
                "monitor_url": monitor_url,
                "expected_gateways": expected_gateways,
            }
        )

    if drills_raw and not isinstance(drills_raw, dict):
        raise ValueError("ha.drills must be a mapping")
    leader_failover_command = str(drills_raw.get("leader_failover_command", "")).strip() or None
    etcd_restart_command = str(drills_raw.get("etcd_restart_command", "")).strip() or None
    transport_recovery_command = (
        str(drills_raw.get("transport_recovery_command", "")).strip() or None
    )
    drills = {
        "leader_failover_command": leader_failover_command,
        "etcd_restart_command": etcd_restart_command,
        "transport_recovery_command": transport_recovery_command,
    }

    return {
        "enabled": has_ha_hosts,
        "etcd_endpoints": etcd_endpoints,
        "etcd_prefix": etcd_prefix or None,
        "nats_url": nats_url or None,
        "hub_nodes": hub_nodes,
        "edge_sites": edge_sites,
        "drills": drills,
        "controller_scheme": controller_scheme,
        "apishim_scheme": apishim_scheme,
        "controller_metrics_url": controller_metrics_url,
        "expected_version": str(use_raw.get("expected_version", "")).strip() or None,
        "expected_sha": str(use_raw.get("expected_sha", "")).strip() or None,
    }


def _parse_smoke(raw: dict[str, Any]) -> dict[str, Any]:
    lanes = list(DEFAULT_SMOKE_LANES)
    phase_timeouts = dict(DEFAULT_PHASE_TIMEOUTS)
    retry_policy = dict(DEFAULT_RETRY_POLICY)
    checks = dict(DEFAULT_SMOKE_CHECKS)

    if not raw:
        return {
            "lanes": lanes,
            "defaults": {
                "phase_timeouts": phase_timeouts,
                "retry_policy": retry_policy,
            },
            "checks": checks,
        }

    if not isinstance(raw, dict):
        raise ValueError("smoke must be a mapping")

    raw_lanes = raw.get("lanes")
    if raw_lanes is not None:
        if not isinstance(raw_lanes, list):
            raise ValueError("smoke.lanes must be a list")
        lanes = []
        for entry in raw_lanes:
            if not isinstance(entry, str):
                raise ValueError("smoke.lanes entries must be strings")
            lane = entry.strip()
            if lane not in SUPPORTED_SMOKE_LANES:
                raise ValueError(f"smoke.lanes contains unsupported lane: {lane}")
            lanes.append(lane)
        if not lanes:
            raise ValueError("smoke.lanes must not be empty")

    defaults = raw.get("defaults")
    if defaults is not None:
        if not isinstance(defaults, dict):
            raise ValueError("smoke.defaults must be a mapping")

        raw_timeouts = defaults.get("phase_timeouts")
        if raw_timeouts is not None:
            if not isinstance(raw_timeouts, dict):
                raise ValueError("smoke.defaults.phase_timeouts must be a mapping")
            for key, value in raw_timeouts.items():
                if key not in phase_timeouts:
                    raise ValueError(f"unsupported smoke phase timeout key: {key}")
                seconds = int(value)
                if seconds <= 0:
                    raise ValueError(f"smoke.defaults.phase_timeouts.{key} must be > 0")
                phase_timeouts[key] = seconds

        raw_retry = defaults.get("retry_policy")
        if raw_retry is not None:
            if not isinstance(raw_retry, dict):
                raise ValueError("smoke.defaults.retry_policy must be a mapping")
            for key in retry_policy:
                if key in raw_retry:
                    retry_policy[key] = float(raw_retry[key])
                    if retry_policy[key] < 0:
                        raise ValueError(f"smoke.defaults.retry_policy.{key} must be >= 0")

    raw_checks = raw.get("checks")
    if raw_checks is not None:
        if not isinstance(raw_checks, dict):
            raise ValueError("smoke.checks must be a mapping")
        for key, value in raw_checks.items():
            if key not in checks:
                raise ValueError(f"unsupported smoke.checks key: {key}")
            checks[key] = bool(value)

    return {
        "lanes": lanes,
        "defaults": {
            "phase_timeouts": phase_timeouts,
            "retry_policy": retry_policy,
        },
        "checks": checks,
    }


def parse_variant(path: Path, *, validate_images: bool = False) -> dict[str, Any]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("variant root must be a mapping")

    name = _must(raw, "name", str, "variant")
    test_id = int(_must(raw, "test_id", int, "variant"))

    net = _must(raw, "network", dict, "variant")
    bridge = _must(net, "bridge", str, "network")
    cidr = _must(net, "cidr", str, "network")
    gateway = _must(net, "gateway", str, "network")

    vm = _must(raw, "vm", dict, "variant")
    memory_mb = int(vm.get("memory_mb", 4096))
    vcpus = int(vm.get("vcpus", 4))
    disk_gb = int(vm.get("disk_gb", 30))

    images = _must(raw, "images", dict, "variant")
    base_img = _resolve_repo_path(_must(images, "base", str, "images"))
    gpu_img = _resolve_repo_path(_must(images, "gpu", str, "images"))
    if validate_images:
        for image_name, image_path in (("base", base_img), ("gpu", gpu_img)):
            if not Path(image_path).is_file():
                raise ValueError(f"images.{image_name} not found: {image_path}")

    hosts_raw = _must(raw, "hosts", list, "variant")
    if not hosts_raw:
        raise ValueError("hosts must contain at least one item")
    hosts = [_normalize_host(item, i) for i, item in enumerate(hosts_raw)]

    k1s = raw.get("k1s") or {}
    if not isinstance(k1s, dict):
        raise ValueError("k1s must be a mapping")

    baseline = raw.get("baseline") or {}
    if not isinstance(baseline, dict):
        raise ValueError("baseline must be a mapping")

    gate = raw.get("throughput_gate") or {}
    if not isinstance(gate, dict):
        raise ValueError("throughput_gate must be a mapping")

    transport = raw.get("transport") or {}
    if not isinstance(transport, dict):
        raise ValueError("transport must be a mapping")
    leaf_uplink_mode = str(transport.get("leaf_uplink_mode", "direct_ip")).strip().lower()
    if leaf_uplink_mode not in {"direct_ip", "local_tunnel"}:
        raise ValueError("transport.leaf_uplink_mode must be direct_ip or local_tunnel")
    hub_host = str(transport.get("hub_host", "")).strip() or None
    hub_leaf_port = int(transport.get("hub_leaf_port", 7422))
    if hub_leaf_port <= 0:
        raise ValueError("transport.hub_leaf_port must be > 0")

    raw_smoke = raw.get("smoke") or {}
    smoke = _parse_smoke(raw_smoke)
    ha = _parse_ha(raw.get("ha") or {}, hosts)
    if ha["enabled"] and not (isinstance(raw_smoke, dict) and raw_smoke.get("lanes") is not None):
        smoke["lanes"] = [*smoke["lanes"], "ha_control_plane"]

    environments = raw.get("environments") or {}
    if not isinstance(environments, dict):
        raise ValueError("environments must be a mapping")
    local_vm = environments.get("local_vm") or {}
    remote_lab = environments.get("remote_lab") or {}
    if not isinstance(local_vm, dict):
        raise ValueError("environments.local_vm must be a mapping")
    if not isinstance(remote_lab, dict):
        raise ValueError("environments.remote_lab must be a mapping")

    secrets = raw.get("secrets") or {}
    if not isinstance(secrets, dict):
        raise ValueError("secrets must be a mapping")
    refs = secrets.get("refs") or {}
    if not isinstance(refs, dict):
        raise ValueError("secrets.refs must be a mapping")
    secret_refs = {}
    for key, value in refs.items():
        if not isinstance(key, str):
            raise ValueError("secrets.refs keys must be strings")
        if not isinstance(value, str):
            raise ValueError("secrets.refs values must be strings")
        clean_key = key.strip()
        clean_value = value.strip()
        if not clean_key or not clean_value:
            raise ValueError("secrets.refs entries must be non-empty strings")
        secret_refs[clean_key] = clean_value

    return {
        "name": name,
        "test_id": test_id,
        "description": str(raw.get("description", "")).strip(),
        "network": {
            "bridge": bridge,
            "cidr": cidr,
            "gateway": gateway,
            "dns": list(net.get("dns", ["1.1.1.1", "8.8.8.8"])),
        },
        "vm": {
            "memory_mb": memory_mb,
            "vcpus": vcpus,
            "disk_gb": disk_gb,
        },
        "images": {
            "base": base_img,
            "gpu": gpu_img,
        },
        "hosts": hosts,
        "k1s": {
            "agent_token": str(k1s.get("agent_token", "devtoken")),
            "controller_port": int(k1s.get("controller_port", 9108)),
            "apishim_port": int(k1s.get("apishim_port", 8445)),
            "controller_host": str(k1s.get("controller_host", "")) or None,
            "inference_experimental": bool(k1s.get("inference_experimental", True)),
        },
        "baseline": {
            "duration_minutes": int(baseline.get("duration_minutes", 20)),
            "concurrency": int(baseline.get("concurrency", 16)),
            "max_error_rate": float(baseline.get("max_error_rate", 0.001)),
        },
        "throughput_gate": {
            "min_tps_ratio": float(gate.get("min_tps_ratio", 0.90)),
            "max_p95_ratio": float(gate.get("max_p95_ratio", 1.20)),
            "max_error_rate": float(gate.get("max_error_rate", 0.001)),
        },
        "transport": {
            "leaf_uplink_mode": leaf_uplink_mode,
            "hub_host": hub_host,
            "hub_leaf_port": hub_leaf_port,
        },
        "ha": ha,
        "smoke": smoke,
        "environments": {
            "local_vm": local_vm,
            "remote_lab": remote_lab,
        },
        "secrets": {
            "refs": secret_refs,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate and print VM test variant")
    parser.add_argument("--variant", required=True, help="Path to variant yaml")
    parser.add_argument("--print-json", action="store_true", help="Print normalized JSON")
    parser.add_argument("--get", default="", help="Dot path lookup")
    parser.add_argument(
        "--validate-images",
        action="store_true",
        help="Validate that resolved image paths exist on disk",
    )
    args = parser.parse_args()

    variant = parse_variant(Path(args.variant), validate_images=args.validate_images)

    if args.get:
        value: Any = variant
        for part in args.get.split("."):
            if isinstance(value, dict):
                value = value[part]
            elif isinstance(value, list):
                value = value[int(part)]
            else:
                raise KeyError(args.get)
        if isinstance(value, dict | list):
            print(json.dumps(value, indent=2, sort_keys=True))
        else:
            print(value)
        return 0

    if args.print_json:
        print(json.dumps(variant, indent=2, sort_keys=True))
        return 0

    print(f"variant ok: {variant['name']}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # pragma: no cover
        print(f"variant validation failed: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
