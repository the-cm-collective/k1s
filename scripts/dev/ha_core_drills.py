#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
import urllib.request
from urllib.parse import urlsplit, urlunsplit

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from ae.ha.ops import (  # noqa: E402
    parse_prometheus_metric_value,
    read_etcd_leader,
    split_csv,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ha_core_drills.py",
        description="Run focused HA verification drills against k1s-ha-core",
    )
    sub = parser.add_subparsers(dest="command_name", required=True)

    leader = sub.add_parser("leader-failover", help="Verify leader change after a controller disruption")
    _add_command_arg(leader)
    _add_dry_run_arg(leader)
    _add_etcd_args(leader)
    leader.add_argument("--timeout-seconds", type=float, default=45.0)
    leader.add_argument("--require-controller-change", action="store_true")

    etcd = sub.add_parser("etcd-restart", help="Verify controller recovery after an etcd restart or leader move")
    _add_command_arg(etcd)
    _add_dry_run_arg(etcd)
    _add_etcd_args(etcd)
    etcd.add_argument("--metrics-url", default=os.getenv("HA_CORE_METRICS_URL") or "")
    etcd.add_argument("--timeout-seconds", type=float, default=45.0)

    transport = sub.add_parser("transport-recovery", help="Verify gateway replay and route convergence after transport impairment")
    _add_command_arg(transport)
    _add_dry_run_arg(transport)
    _add_etcd_args(transport)
    transport.add_argument("--metrics-url", default=os.getenv("HA_CORE_METRICS_URL") or "")
    transport.add_argument("--site", default=os.getenv("HA_CORE_SITE_ID") or "")
    transport.add_argument("--timeout-seconds", type=float, default=45.0)
    transport.add_argument("--backlog-threshold", type=float, default=0.0)
    transport.add_argument("--ack-age-threshold", type=float, default=15.0)
    return parser


def _add_command_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--command", required=True, help="Shell command that triggers the drill condition")


def _add_dry_run_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--dry-run", action="store_true", help="Print what would be checked without executing the drill")


def _add_etcd_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--etcd-endpoints", default=os.getenv("AE_ETCD_ENDPOINTS") or "")
    parser.add_argument("--etcd-prefix", default=os.getenv("AE_ETCD_PREFIX") or "")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command_name == "leader-failover":
        return leader_failover(args)
    if args.command_name == "etcd-restart":
        return etcd_restart(args)
    return transport_recovery(args)


def leader_failover(args: argparse.Namespace) -> int:
    endpoints = split_csv(args.etcd_endpoints)
    prefix = str(args.etcd_prefix or "").strip()
    if args.dry_run:
        print(
            f"DRY RUN leader-failover command={args.command!r} timeout={args.timeout_seconds}s "
            f"require_controller_change={bool(args.require_controller_change)}"
        )
        return 0
    if not endpoints or not prefix:
        raise SystemExit("--etcd-endpoints and --etcd-prefix are required")
    before = read_etcd_leader(endpoints, prefix, timeout_s=3.0)
    if before is None:
        raise SystemExit("no current controller leader record found")
    _run_shell(args.command)
    deadline = time.time() + float(args.timeout_seconds)
    while time.time() < deadline:
        after = read_etcd_leader(endpoints, prefix, timeout_s=3.0)
        if after is not None and after.controller_epoch > before.controller_epoch:
            if args.require_controller_change and after.controller_id == before.controller_id:
                time.sleep(1.0)
                continue
            print(
                f"leader failover observed: before={before.controller_id}:{before.controller_epoch} "
                f"after={after.controller_id}:{after.controller_epoch}"
            )
            return 0
        time.sleep(1.0)
    raise SystemExit("leader failover was not observed before timeout")


def etcd_restart(args: argparse.Namespace) -> int:
    endpoints = split_csv(args.etcd_endpoints)
    prefix = str(args.etcd_prefix or "").strip()
    fallback_metrics_url = str(args.metrics_url or "").strip()
    if args.dry_run:
        print(
            f"DRY RUN etcd-restart command={args.command!r} timeout={args.timeout_seconds}s "
            f"metrics_url={fallback_metrics_url or 'n/a'}"
        )
        return 0
    if not endpoints or not prefix:
        raise SystemExit("--etcd-endpoints and --etcd-prefix are required")
    _run_shell(args.command)
    deadline = time.time() + float(args.timeout_seconds)
    while time.time() < deadline:
        leader = _read_current_leader(endpoints, prefix)
        resolved_metrics_url = _resolve_metrics_url(
            fallback_metrics_url,
            leader=leader,
        )
        metrics_ok = True
        if resolved_metrics_url:
            metrics_ok = _metrics_reachable(resolved_metrics_url)
        if leader is not None and metrics_ok:
            print(
                f"etcd recovery observed: leader={leader.controller_id}:{leader.controller_epoch} "
                f"metrics_url={resolved_metrics_url or 'n/a'}"
            )
            return 0
        time.sleep(1.0)
    raise SystemExit("etcd recovery was not observed before timeout")


def transport_recovery(args: argparse.Namespace) -> int:
    fallback_metrics_url = str(args.metrics_url or "").strip()
    endpoints = split_csv(getattr(args, "etcd_endpoints", ""))
    prefix = str(getattr(args, "etcd_prefix", "") or "").strip()
    if args.dry_run:
        print(
            f"DRY RUN transport-recovery command={args.command!r} timeout={args.timeout_seconds}s "
            f"site={args.site or 'n/a'} backlog_threshold={args.backlog_threshold} "
            f"ack_age_threshold={args.ack_age_threshold}"
        )
        return 0
    if not fallback_metrics_url and (not endpoints or not prefix):
        raise SystemExit("--metrics-url is required when HA discovery args are unavailable")
    _run_shell(args.command)
    deadline = time.time() + float(args.timeout_seconds)
    while time.time() < deadline:
        leader = _read_current_leader(endpoints, prefix)
        metrics_url = _resolve_metrics_url(
            fallback_metrics_url,
            leader=leader,
        )
        if not metrics_url:
            time.sleep(1.0)
            continue
        try:
            metrics = _fetch_metrics(metrics_url)
        except Exception:
            time.sleep(1.0)
            continue
        site = str(args.site or "").strip()
        labels = {"site": site} if site else None
        backlog = parse_prometheus_metric_value(
            metrics,
            "ae_gateway_result_replay_backlog",
            labels=labels,
        )
        ack_age = parse_prometheus_metric_value(
            metrics,
            "ae_route_bundle_ack_age_seconds",
            labels=labels,
        )
        site_stale = parse_prometheus_metric_value(
            metrics,
            "ae_site_stale",
            labels=labels,
        )
        backlog_ok = backlog is None or backlog <= float(args.backlog_threshold)
        ack_age_ok = ack_age is None or ack_age <= float(args.ack_age_threshold)
        stale_ok = site_stale is None or site_stale <= 0.0
        if backlog_ok and ack_age_ok and stale_ok:
            print(
                f"transport recovery observed: backlog={backlog} ack_age={ack_age} "
                f"site_stale={site_stale} site={site or 'n/a'}"
            )
            return 0
        time.sleep(1.0)
    raise SystemExit("transport recovery was not observed before timeout")


def _run_shell(command: str) -> None:
    subprocess.run(  # noqa: S602,S603
        ["bash", "-lc", command],
        check=True,
        text=True,
        capture_output=False,
    )


def _metrics_reachable(url: str) -> bool:
    try:
        _fetch_metrics(url)
    except Exception:
        return False
    return True


def _fetch_metrics(url: str) -> str:
    with urllib.request.urlopen(url, timeout=3.0) as resp:
        return resp.read().decode("utf-8")


def _read_current_leader(endpoints: list[str], prefix: str):
    if not endpoints or not prefix:
        return None
    try:
        return read_etcd_leader(endpoints, prefix, timeout_s=3.0)
    except Exception:
        return None


def _resolve_metrics_url(fallback_url: str, *, leader) -> str:
    leader_url = _metrics_url_from_advertise_addr(getattr(leader, "advertise_addr", None))
    if leader_url:
        return leader_url
    return str(fallback_url or "").strip()


def _metrics_url_from_advertise_addr(advertise_addr: str | None) -> str:
    raw = str(advertise_addr or "").strip()
    if not raw:
        return ""
    parsed = urlsplit(raw if "://" in raw else f"http://{raw}")
    if not parsed.netloc:
        return ""
    return urlunsplit((parsed.scheme or "http", parsed.netloc, "/metrics", "", ""))


if __name__ == "__main__":
    raise SystemExit(main())
