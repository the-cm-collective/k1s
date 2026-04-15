#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from ae.ha.ops import (  # noqa: E402
    etcd_endpoint_healthy,
    ha_core_missing_env,
    healthy_etcd_endpoints,
    is_loopback_host,
    parse_nats_url,
    split_csv,
    tcp_connectable,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ha_core_preflight.py",
        description="Validate external/shared HA dependencies for k1s-ha-core",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=float(os.getenv("HA_CORE_PREFLIGHT_TIMEOUT_SECONDS", "3") or 3),
        help="Per-endpoint connectivity timeout",
    )
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    env = dict(os.environ)
    timeout_s = max(0.2, float(args.timeout_seconds))
    errors: list[str] = []
    warnings: list[str] = []

    missing = ha_core_missing_env(env)
    if missing:
        errors.append(f"missing required env: {', '.join(missing)}")

    etcd_endpoints = split_csv(env.get("AE_ETCD_ENDPOINTS"))
    healthy = healthy_etcd_endpoints(etcd_endpoints, timeout_s=timeout_s) if etcd_endpoints else []
    if etcd_endpoints and not healthy:
        details = []
        for endpoint in etcd_endpoints:
            ok, detail = etcd_endpoint_healthy(endpoint, timeout_s=timeout_s)
            if not ok:
                details.append(f"{endpoint} ({detail})")
        errors.append(f"no healthy etcd endpoints: {', '.join(details)}")
    if etcd_endpoints and all(is_loopback_host(_host_from_http_url(endpoint)) for endpoint in etcd_endpoints):
        if str(env.get("AE_DEV_LOCAL", "0")).strip().lower() != "1":
            warnings.append("all etcd endpoints are loopback; production k1s-ha-core normally uses shared external etcd")

    nats_urls = split_csv(env.get("AE_NATS_URL"))
    nats_reachable = 0
    nats_failures: list[str] = []
    for item in nats_urls:
        try:
            host, port = parse_nats_url(item)
        except ValueError as exc:
            nats_failures.append(f"{item} ({exc})")
            continue
        ok, detail = tcp_connectable(host, port, timeout_s=timeout_s)
        if ok:
            nats_reachable += 1
        else:
            nats_failures.append(f"{item} ({detail})")
    if nats_urls and nats_reachable == 0:
        errors.append(f"no reachable NATS endpoints: {', '.join(nats_failures)}")
    if nats_urls and all(is_loopback_host(_host_from_nats_url(item)) for item in nats_urls):
        if str(env.get("AE_DEV_LOCAL", "0")).strip().lower() != "1":
            warnings.append("all NATS endpoints are loopback; production k1s-ha-core normally uses shared external NATS")

    if str(env.get("AE_TRANSPORT_BACKEND") or "").strip().lower() not in {"", "nats-js"}:
        warnings.append("AE_TRANSPORT_BACKEND is not nats-js; k1s-ha-core defaults to JetStream-backed HA transport")
    if str(env.get("AE_ETCD_MAINTENANCE_ENABLE") or "").strip() not in {"", "0", "false", "False"}:
        warnings.append("AE_ETCD_MAINTENANCE_ENABLE is on; external/shared etcd maintenance should be operator-managed")

    result = {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "healthy_etcd_endpoints": healthy,
        "reachable_nats_urls": nats_reachable,
    }
    if args.json:
        print(json.dumps(result, sort_keys=True))
    else:
        for line in errors:
            print(f"ERROR: {line}")
        for line in warnings:
            print(f"WARN: {line}")
        if not errors:
            etcd_summary = ", ".join(healthy) if healthy else "n/a"
            print(f"HA preflight OK: etcd={etcd_summary} nats_reachable={nats_reachable}")
    return 0 if not errors else 1


def _host_from_http_url(url: str) -> str | None:
    raw = str(url or "").strip()
    if "://" not in raw:
        raw = f"http://{raw}"
    try:
        from urllib.parse import urlparse

        return urlparse(raw).hostname
    except Exception:
        return None


def _host_from_nats_url(url: str) -> str | None:
    try:
        host, _ = parse_nats_url(url)
        return host
    except Exception:
        return None


if __name__ == "__main__":
    raise SystemExit(main())
