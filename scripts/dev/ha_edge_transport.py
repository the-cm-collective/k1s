#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from ae.ha.ops import (  # noqa: E402
    collect_prometheus_metric_values,
    collect_site_gateway_status,
    evaluate_nats_edge_site,
    fetch_http_text,
    fetch_nats_edge_monitor_record,
    parse_nats_edge_site_target,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ha_edge_transport.py",
        description="Plan and validate edge-site transport upgrades for k1s HA transport",
    )
    sub = parser.add_subparsers(dest="command_name", required=True)

    precheck = sub.add_parser(
        "precheck",
        help="Validate one edge site before gateway or edge-leader maintenance",
    )
    _add_site_args(precheck)
    precheck.add_argument("--dry-run", action="store_true")

    gateway_plan = sub.add_parser(
        "gateway-plan",
        help="Print the ordered restart steps for one gateway in an edge site",
    )
    gateway_plan.add_argument("--site-id", required=True)
    gateway_plan.add_argument("--gateway-node", required=True)
    gateway_plan.add_argument(
        "--restart-command",
        default=os.getenv("HA_EDGE_GATEWAY_RESTART_COMMAND") or "<operator-managed gateway restart command>",
    )
    gateway_plan.add_argument("--controller-metrics-url", default=os.getenv("HA_CORE_METRICS_URL") or "")
    gateway_plan.add_argument("--expected-version", default="")
    gateway_plan.add_argument("--expected-sha", default="")

    verify = sub.add_parser(
        "site-verify",
        help="Verify one edge site after gateway or edge-leader maintenance",
    )
    _add_site_args(verify)
    verify.add_argument("--expected-edge-version", default="")
    verify.add_argument("--expected-edge-commit", default="")
    verify.add_argument("--expected-gateway-version", default="")
    verify.add_argument("--expected-gateway-sha", default="")
    verify.add_argument("--require-gateway-converged", action="store_true")

    leader_plan = sub.add_parser(
        "leader-plan",
        help="Print the gateway-first / edge-leader-last restart sequence for one site",
    )
    leader_plan.add_argument("--site-id", required=True)
    leader_plan.add_argument(
        "--monitor-url",
        default=os.getenv("HA_EDGE_NATS_MONITOR_URL") or "http://127.0.0.1:8223",
    )
    leader_plan.add_argument(
        "--restart-command",
        default=os.getenv("HA_EDGE_NATS_RESTART_COMMAND") or "<operator-managed edge NATS restart command>",
    )
    leader_plan.add_argument("--controller-metrics-url", default=os.getenv("HA_CORE_METRICS_URL") or "")
    leader_plan.add_argument("--expected-version", default="")
    leader_plan.add_argument("--expected-commit", default="")

    replace = sub.add_parser(
        "leader-replace-plan",
        help="Print the decision-complete checklist for replacing one failed edge NATS leader host",
    )
    replace.add_argument("--site-id", required=True)
    replace.add_argument("--failed-node", required=True)
    replace.add_argument("--replacement-node", required=True)
    replace.add_argument("--replacement-monitor-url", default="")
    replace.add_argument("--controller-metrics-url", default=os.getenv("HA_CORE_METRICS_URL") or "")
    replace.add_argument("--expected-gateway", action="append", default=[])
    replace.add_argument("--leaf-min-count", type=int, default=1)
    return parser


def _add_site_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--site",
        required=True,
        metavar="SITE_ID=MONITOR_URL",
        help="Edge site identifier and edge leader NATS monitor URL",
    )
    parser.add_argument("--controller-metrics-url", default=os.getenv("HA_CORE_METRICS_URL") or "")
    parser.add_argument("--expected-gateway", action="append", default=[])
    parser.add_argument("--leaf-min-count", type=int, default=1)
    parser.add_argument("--gateway-last-seen-threshold", type=float, default=90.0)
    parser.add_argument("--backlog-threshold", type=float, default=0.0)
    parser.add_argument("--ack-age-threshold", type=float, default=15.0)
    parser.add_argument("--timeout-seconds", type=float, default=3.0)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command_name == "precheck":
        return precheck(args)
    if args.command_name == "gateway-plan":
        return gateway_plan(args)
    if args.command_name == "site-verify":
        return site_verify(args)
    if args.command_name == "leader-plan":
        return leader_plan(args)
    return leader_replace_plan(args)


def precheck(args: argparse.Namespace) -> int:
    expected_gateways = _expected_gateways(args.expected_gateway)
    if args.dry_run:
        print(
            "DRY RUN precheck "
            f"site={args.site} expected_gateways={len(expected_gateways)} "
            f"leaf_min={int(args.leaf_min_count)} "
            f"gateway_last_seen_threshold={float(args.gateway_last_seen_threshold)} "
            f"backlog_threshold={float(args.backlog_threshold)} "
            f"ack_age_threshold={float(args.ack_age_threshold)}"
        )
        return 0
    target, edge_record, metrics_text, gateway_statuses = _load_site_state(args, expected_gateways=expected_gateways)
    issues = _site_issues(
        edge_record=edge_record,
        metrics_text=metrics_text,
        site_id=target.site_id,
        expected_gateways=expected_gateways,
        gateway_statuses=gateway_statuses,
        gateway_last_seen_threshold=float(args.gateway_last_seen_threshold),
        backlog_threshold=float(args.backlog_threshold),
        ack_age_threshold=float(args.ack_age_threshold),
        expected_leaf_min=int(args.leaf_min_count),
    )
    if issues:
        raise SystemExit("edge transport precheck failed: " + "; ".join(issues))
    distinct_gateway_builds = _distinct_gateway_builds(gateway_statuses, expected_gateways)
    print(
        "edge transport precheck ok: "
        f"site={target.site_id} gateways={len(expected_gateways)} leaf_count={edge_record.leaf_count or 0} "
        f"distinct_gateway_builds={len(distinct_gateway_builds)}"
    )
    return 0


def gateway_plan(args: argparse.Namespace) -> int:
    print(f"Edge gateway rolling plan for site {args.site_id} gateway {args.gateway_node}")
    print("1. Run edge transport precheck and confirm the site is healthy before touching this gateway.")
    print("2. Restart one gateway at a time; keep the edge NATS leader untouched until all gateway work is complete.")
    print(f"3. Restart the operator-managed gateway using: {args.restart_command}")
    if args.controller_metrics_url:
        base = args.controller_metrics_url.rstrip("/")
        print(
            "4. Verify gateway telemetry and site recovery: "
            f"curl -fsS {base} | "
            f"rg 'ae_site_gateway_last_seen_seconds{{site=\"{args.site_id}\",node=\"{args.gateway_node}\"}}|"
            f"ae_site_gateway_build_info{{site=\"{args.site_id}\",node=\"{args.gateway_node}\"|"
            f"ae_site_stale{{site=\"{args.site_id}\"}}|"
            f"ae_gateway_result_replay_backlog{{site=\"{args.site_id}\"}}|"
            f"ae_route_bundle_ack_age_seconds{{site=\"{args.site_id}\"}}'"
        )
    if args.expected_version or args.expected_sha:
        print(
            f"Expected gateway build: version={args.expected_version or 'n/a'} "
            f"sha={args.expected_sha or 'n/a'}"
        )
    return 0


def site_verify(args: argparse.Namespace) -> int:
    expected_gateways = _expected_gateways(args.expected_gateway)
    target, edge_record, metrics_text, gateway_statuses = _load_site_state(args, expected_gateways=expected_gateways)
    issues = _site_issues(
        edge_record=edge_record,
        metrics_text=metrics_text,
        site_id=target.site_id,
        expected_gateways=expected_gateways,
        gateway_statuses=gateway_statuses,
        gateway_last_seen_threshold=float(args.gateway_last_seen_threshold),
        backlog_threshold=float(args.backlog_threshold),
        ack_age_threshold=float(args.ack_age_threshold),
        expected_leaf_min=int(args.leaf_min_count),
    )
    issues.extend(
        _gateway_build_issues(
            gateway_statuses,
            expected_gateways=expected_gateways,
            expected_version=str(args.expected_gateway_version or "").strip(),
            expected_sha=str(args.expected_gateway_sha or "").strip(),
            require_converged=bool(args.require_gateway_converged),
        )
    )
    expected_edge_version = str(args.expected_edge_version or "").strip()
    expected_edge_commit = str(args.expected_edge_commit or "").strip()
    if expected_edge_version and edge_record.version != expected_edge_version:
        issues.append(f"edge_build_version:{edge_record.version or 'unknown'}/{expected_edge_version}")
    if expected_edge_commit and edge_record.git_commit != expected_edge_commit:
        issues.append(f"edge_build_commit:{edge_record.git_commit or 'unknown'}/{expected_edge_commit}")
    if issues:
        raise SystemExit("edge transport site verify failed: " + "; ".join(issues))

    lines = [
        f"edge-site={target.site_id}: version={edge_record.version or 'unknown'} "
        f"commit={edge_record.git_commit or 'unknown'} leaf_count={edge_record.leaf_count or 0}"
    ]
    for gateway_name in expected_gateways:
        record = gateway_statuses[gateway_name]
        lines.append(
            f"{gateway_name}: last_seen={record.last_seen_seconds} "
            f"version={record.version or 'unknown'} sha={record.sha or 'unknown'}"
        )
    print("\n".join(lines))
    print(
        "edge transport site verify ok: "
        f"site={target.site_id} gateways={len(expected_gateways)} "
        f"distinct_gateway_builds={len(_distinct_gateway_builds(gateway_statuses, expected_gateways))}"
    )
    return 0


def leader_plan(args: argparse.Namespace) -> int:
    print(f"Edge transport leader restart plan for site {args.site_id}")
    print("1. Run edge transport precheck and complete any gateway upgrades before touching the edge NATS leader.")
    print("2. Restart gateways one at a time first; the edge NATS leader is restarted last.")
    print(f"3. Restart the operator-managed edge NATS leader using: {args.restart_command}")
    print(f"4. Verify edge NATS build: curl -fsS {args.monitor_url.rstrip('/')}/varz")
    print(f"5. Verify hub leaf reconnect: curl -fsS {args.monitor_url.rstrip('/')}/leafz")
    if args.controller_metrics_url:
        print(
            "6. Verify controller transport metrics for the site: "
            f"curl -fsS {args.controller_metrics_url.rstrip('/')} | "
            f"rg 'ae_site_stale{{site=\"{args.site_id}\"}}|"
            f"ae_gateway_result_replay_backlog{{site=\"{args.site_id}\"}}|"
            f"ae_route_bundle_ack_age_seconds{{site=\"{args.site_id}\"}}|"
            f"ae_site_gateway_'"
        )
    if args.expected_version or args.expected_commit:
        print(
            f"Expected edge NATS build: version={args.expected_version or 'n/a'} "
            f"commit={args.expected_commit or 'n/a'}"
        )
    return 0


def leader_replace_plan(args: argparse.Namespace) -> int:
    print(f"Edge transport leader replacement plan for site {args.site_id}")
    print(f"1. Confirm the failed edge leader is isolated: {args.failed_node}")
    print("2. Confirm the shared hub transport remains healthy before introducing the replacement edge leader.")
    print(
        "3. Bring up the replacement edge leader with the existing operator-managed edge NATS config, "
        "site identity, leaf uplink, and auth settings."
    )
    if args.replacement_monitor_url:
        print(f"4. Verify replacement edge NATS build: curl -fsS {args.replacement_monitor_url.rstrip('/')}/varz")
        print(f"5. Verify replacement hub leaf reconnect: curl -fsS {args.replacement_monitor_url.rstrip('/')}/leafz")
        next_step = 6
    else:
        next_step = 4
    if args.controller_metrics_url:
        print(
            f"{next_step}. Verify the site recovered through controller metrics: "
            f"curl -fsS {args.controller_metrics_url.rstrip('/')} | "
            f"rg 'ae_site_stale{{site=\"{args.site_id}\"}}|"
            f"ae_gateway_result_replay_backlog{{site=\"{args.site_id}\"}}|"
            f"ae_route_bundle_ack_age_seconds{{site=\"{args.site_id}\"}}|"
            f"ae_site_gateway_'"
        )
        next_step += 1
    if args.expected_gateway:
        print(
            f"{next_step}. Confirm expected gateways are visible again: "
            + ", ".join(sorted({str(item).strip() for item in args.expected_gateway if str(item).strip()}))
        )
    print(
        "Non-goals: this helper does not install edge services, generate NATS configs, "
        "rotate auth, or orchestrate remote restarts."
    )
    return 0


def _load_site_state(
    args: argparse.Namespace,
    *,
    expected_gateways: list[str],
):
    if not expected_gateways:
        raise SystemExit("at least one --expected-gateway is required")
    metrics_url = str(args.controller_metrics_url or "").strip()
    if not metrics_url:
        raise SystemExit("--controller-metrics-url is required")
    target = parse_nats_edge_site_target(args.site)
    timeout_s = float(args.timeout_seconds)
    edge_record = fetch_nats_edge_monitor_record(
        target,
        timeout_s=timeout_s,
        include_leafz=args.leaf_min_count is not None,
    )
    metrics_text = fetch_http_text(metrics_url, timeout_s=timeout_s)
    gateway_statuses = collect_site_gateway_status(metrics_text, target.site_id)
    return target, edge_record, metrics_text, gateway_statuses


def _expected_gateways(raw_values: list[str]) -> list[str]:
    gateways = sorted({str(item).strip() for item in raw_values if str(item).strip()})
    return gateways


def _site_issues(
    *,
    edge_record,
    metrics_text: str,
    site_id: str,
    expected_gateways: list[str],
    gateway_statuses,
    gateway_last_seen_threshold: float,
    backlog_threshold: float,
    ack_age_threshold: float,
    expected_leaf_min: int,
) -> list[str]:
    issues = evaluate_nats_edge_site(edge_record, expected_leaf_min=expected_leaf_min)
    authority_samples = collect_prometheus_metric_values(metrics_text, "ae_controller_authority_healthy")
    if not authority_samples or max(value for _labels, value in authority_samples) < 1:
        issues.append("controller_authority_unhealthy")
    site_stale = _site_metric_value(metrics_text, "ae_site_stale", site_id)
    if site_stale is not None and site_stale > 0:
        issues.append(f"site_stale:{site_id}")
    backlog = _site_metric_value(metrics_text, "ae_gateway_result_replay_backlog", site_id)
    if backlog is not None and backlog > backlog_threshold:
        issues.append(f"gateway_replay_backlog:{backlog}/{backlog_threshold}")
    ack_age = _site_metric_value(metrics_text, "ae_route_bundle_ack_age_seconds", site_id)
    if ack_age is not None and ack_age > ack_age_threshold:
        issues.append(f"route_ack_age:{ack_age}/{ack_age_threshold}")
    for gateway_name in expected_gateways:
        record = gateway_statuses.get(gateway_name)
        if record is None:
            issues.append(f"gateway_missing:{gateway_name}")
            continue
        if record.last_seen_seconds is None:
            issues.append(f"gateway_last_seen_missing:{gateway_name}")
        elif record.last_seen_seconds > gateway_last_seen_threshold:
            issues.append(
                f"gateway_last_seen:{gateway_name}:{record.last_seen_seconds}/{gateway_last_seen_threshold}"
            )
        if not record.version and not record.sha and not record.date:
            issues.append(f"gateway_build_missing:{gateway_name}")
    return issues


def _gateway_build_issues(
    gateway_statuses,
    *,
    expected_gateways: list[str],
    expected_version: str,
    expected_sha: str,
    require_converged: bool,
) -> list[str]:
    issues: list[str] = []
    distinct_builds = _distinct_gateway_builds(gateway_statuses, expected_gateways)
    if len(distinct_builds) > 2:
        issues.append(f"gateway_build_window:{len(distinct_builds)}")
    if expected_version:
        target_seen = False
        non_converged: list[str] = []
        for gateway_name in expected_gateways:
            record = gateway_statuses.get(gateway_name)
            if record is None:
                continue
            matches = record.version == expected_version and (not expected_sha or record.sha == expected_sha)
            if matches:
                target_seen = True
            else:
                non_converged.append(gateway_name)
        if not target_seen:
            issues.append("gateway_target_build_missing")
        if require_converged and non_converged:
            issues.append("gateway_build_not_converged:" + ",".join(non_converged))
    return issues


def _distinct_gateway_builds(gateway_statuses, expected_gateways: list[str]) -> set[tuple[str, str]]:
    builds: set[tuple[str, str]] = set()
    for gateway_name in expected_gateways:
        record = gateway_statuses.get(gateway_name)
        if record is None:
            continue
        if record.version or record.sha:
            builds.add((record.version, record.sha))
    return builds


def _site_metric_value(metrics_text: str, metric_name: str, site_id: str) -> float | None:
    for labels, value in collect_prometheus_metric_values(metrics_text, metric_name):
        if str(labels.get("site") or "").strip() == str(site_id or "").strip():
            return value
    return None


if __name__ == "__main__":
    raise SystemExit(main())
