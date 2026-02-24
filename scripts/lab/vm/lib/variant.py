#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import yaml


def _must(obj: dict[str, Any], key: str, t: type, path: str) -> Any:
    if key not in obj:
        raise ValueError(f"missing {path}.{key}")
    value = obj[key]
    if not isinstance(value, t):
        raise ValueError(f"{path}.{key} must be {t.__name__}")
    return value


def _normalize_host(host: dict[str, Any], idx: int) -> dict[str, Any]:
    name = _must(host, "name", str, f"hosts[{idx}]")
    ip = _must(host, "ip", str, f"hosts[{idx}]")
    role = _must(host, "role", str, f"hosts[{idx}]")
    if role not in {"k1s-core", "k1s-edge-core", "k1s-edge-node"}:
        raise ValueError(
            f"hosts[{idx}].role must be one of k1s-core|k1s-edge-core|k1s-edge-node"
        )
    return {
        "name": name,
        "ip": ip,
        "role": role,
        "gpu": bool(host.get("gpu", False)),
        "site_id": str(host.get("site_id", "")).strip() or None,
        "node_id": str(host.get("node_id", "")).strip() or name,
        "node_labels": str(host.get("node_labels", "")).strip() or None,
        "agent_port": int(host.get("agent_port", 9112 if role != "k1s-core" else 9111)),
    }


def parse_variant(path: Path) -> dict[str, Any]:
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
    base_img = _must(images, "base", str, "images")
    gpu_img = _must(images, "gpu", str, "images")

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
            "controller_port": int(k1s.get("controller_port", 9110)),
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
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate and print VM test variant")
    parser.add_argument("--variant", required=True, help="Path to variant yaml")
    parser.add_argument("--print-json", action="store_true", help="Print normalized JSON")
    parser.add_argument("--get", default="", help="Dot path lookup")
    args = parser.parse_args()

    variant = parse_variant(Path(args.variant))

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
