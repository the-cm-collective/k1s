#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from ae.config.transport import desired_js_replicas  # noqa: E402
from ae.ha.ops import (  # noqa: E402
    collect_prometheus_metric_values,
    evaluate_nats_hub_cluster,
    fetch_http_text,
    fetch_nats_hub_monitor_record,
    nats_build_key,
    parse_nats_hub_node_target,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ha_transport_upgrade.py",
        description="Plan and validate shared hub NATS/JetStream upgrades for k1s HA transport",
    )
    sub = parser.add_subparsers(dest="command_name", required=True)

    precheck = sub.add_parser(
        "precheck",
        help="Validate shared hub NATS/JetStream health plus controller transport metrics before upgrade",
    )
    _add_cluster_args(precheck)
    precheck.add_argument("--dry-run", action="store_true")

    node_plan = sub.add_parser(
        "node-plan",
        help="Print the ordered rolling-upgrade steps for one hub NATS node",
    )
    node_plan.add_argument("--node-name", required=True)
    node_plan.add_argument("--monitor-url", default=os.getenv("HA_HUB_NATS_MONITOR_URL") or "http://127.0.0.1:8222")
    node_plan.add_argument(
        "--restart-command",
        default=os.getenv("HA_HUB_NATS_RESTART_COMMAND") or "sudo systemctl restart nats-server.service",
    )
    node_plan.add_argument("--controller-metrics-url", default=os.getenv("HA_CORE_METRICS_URL") or "")
    node_plan.add_argument("--expected-version", default="")
    node_plan.add_argument("--expected-commit", default="")
    node_plan.add_argument("--meta-leader", action="store_true")

    verify = sub.add_parser(
        "cluster-verify",
        help="Verify hub NATS/JetStream build window, replication, and controller transport health",
    )
    _add_cluster_args(verify)
    verify.add_argument("--expected-version", required=True)
    verify.add_argument("--expected-commit", default="")
    verify.add_argument("--require-converged", action="store_true")

    replace = sub.add_parser(
        "member-replace-plan",
        help="Print the decision-complete checklist for replacing one failed hub NATS node",
    )
    replace.add_argument("--failed-node", required=True)
    replace.add_argument("--replacement-node", required=True)
    replace.add_argument("--replacement-monitor-url", default="")
    replace.add_argument("--controller-metrics-url", default=os.getenv("HA_CORE_METRICS_URL") or "")
    replace.add_argument("--expected-js-domain", default=os.getenv("AE_JS_DOMAIN") or "K1S")
    replace.add_argument("--expected-stream", default=os.getenv("AE_JS_STREAM_NAME") or "K1S_WORK")
    replace.add_argument(
        "--expected-replicas",
        type=int,
        default=desired_js_replicas(os.environ),
    )
    replace.add_argument("--leaf-min-count", type=int, default=None)
    return parser


def _add_cluster_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--node",
        action="append",
        default=[],
        metavar="NAME=MONITOR_URL",
        help="Hub NATS node name and monitor URL; repeat once per hub node",
    )
    parser.add_argument("--controller-metrics-url", default=os.getenv("HA_CORE_METRICS_URL") or "")
    parser.add_argument("--expected-js-domain", default=os.getenv("AE_JS_DOMAIN") or "K1S")
    parser.add_argument("--expected-stream", default=os.getenv("AE_JS_STREAM_NAME") or "K1S_WORK")
    parser.add_argument(
        "--expected-replicas",
        type=int,
        default=desired_js_replicas(os.environ),
    )
    parser.add_argument("--leaf-min-count", type=int, default=None)
    parser.add_argument("--backlog-threshold", type=float, default=0.0)
    parser.add_argument("--ack-age-threshold", type=float, default=15.0)
    parser.add_argument("--timeout-seconds", type=float, default=3.0)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command_name == "precheck":
        return precheck(args)
    if args.command_name == "node-plan":
        return node_plan(args)
    if args.command_name == "cluster-verify":
        return cluster_verify(args)
    return member_replace_plan(args)


def precheck(args: argparse.Namespace) -> int:
    if args.dry_run:
        print(
            "DRY RUN precheck "
            f"nodes={len(args.node)} domain={args.expected_js_domain} stream={args.expected_stream} "
            f"replicas={int(args.expected_replicas)} backlog_threshold={float(args.backlog_threshold)} "
            f"ack_age_threshold={float(args.ack_age_threshold)}"
        )
        return 0
    records, metrics_text, expected_consumers = _load_cluster_state(args)
    issues = evaluate_nats_hub_cluster(
        records,
        expected_domain=str(args.expected_js_domain),
        expected_stream=str(args.expected_stream),
        expected_replicas=int(args.expected_replicas),
        expected_consumers=expected_consumers,
        expected_leaf_min=args.leaf_min_count,
    )
    issues.extend(
        _transport_metric_issues(
            metrics_text,
            backlog_threshold=float(args.backlog_threshold),
            ack_age_threshold=float(args.ack_age_threshold),
        )
    )
    if issues:
        raise SystemExit("hub transport precheck failed: " + "; ".join(issues))
    builds = sorted({nats_build_key(record) for record in records})
    print(
        "hub transport precheck ok: "
        f"nodes={len(records)} distinct_builds={len(builds)} "
        f"expected_consumers={len(expected_consumers)}"
    )
    return 0


def node_plan(args: argparse.Namespace) -> int:
    role = "meta leader" if bool(args.meta_leader) else "non-meta leader"
    print(f"Hub transport rolling upgrade plan for node {args.node_name} ({role})")
    print("1. Run hub transport precheck and confirm controller transport metrics are healthy.")
    if args.meta_leader:
        print("2. Confirm all non-meta-leader hub nodes already report the target build before touching this node.")
    else:
        print("2. Confirm this node is not the JetStream meta leader before restart.")
    print(f"3. Restart the node using the operator-managed command: {args.restart_command}")
    print(f"4. Verify NATS version/build: curl -fsS {args.monitor_url.rstrip('/')}/varz")
    print(f"5. Verify route mesh re-formed: curl -fsS {args.monitor_url.rstrip('/')}/routez")
    print(
        "6. Verify JetStream cluster/replicas: "
        f"curl -fsS '{args.monitor_url.rstrip('/')}/jsz?streams=true&consumers=true&config=true'"
    )
    if args.controller_metrics_url:
        print(
            "7. Verify controller transport metrics: "
            f"curl -fsS {args.controller_metrics_url.rstrip('/')} | "
            "rg 'ae_gateway_result_replay_backlog|ae_route_bundle_ack_age_seconds|ae_site_stale|ae_js_'"
        )
    if args.expected_version or args.expected_commit:
        print(
            f"Expected NATS build: version={args.expected_version or 'n/a'} "
            f"commit={args.expected_commit or 'n/a'}"
        )
    return 0


def cluster_verify(args: argparse.Namespace) -> int:
    records, metrics_text, expected_consumers = _load_cluster_state(args)
    issues = evaluate_nats_hub_cluster(
        records,
        expected_domain=str(args.expected_js_domain),
        expected_stream=str(args.expected_stream),
        expected_replicas=int(args.expected_replicas),
        expected_consumers=expected_consumers,
        expected_leaf_min=args.leaf_min_count,
    )
    issues.extend(
        _transport_metric_issues(
            metrics_text,
            backlog_threshold=float(args.backlog_threshold),
            ack_age_threshold=float(args.ack_age_threshold),
        )
    )
    if issues:
        raise SystemExit("hub transport cluster verify failed: " + "; ".join(issues))

    distinct_builds: set[tuple[str, str]] = set()
    target_seen = False
    lines: list[str] = []
    for record in records:
        build = nats_build_key(record)
        distinct_builds.add(build)
        if _matches_expected(record, args.expected_version, args.expected_commit):
            target_seen = True
        lines.append(
            f"{record.name}: version={record.version or 'unknown'} "
            f"commit={record.git_commit or 'n/a'} routes={record.route_count} "
            f"meta_leader={record.meta_leader or 'n/a'}"
        )

    if len(distinct_builds) > 2:
        raise SystemExit(f"hub transport upgrade window exceeded: found {len(distinct_builds)} distinct builds")
    if not target_seen:
        raise SystemExit("target NATS build is not present on any hub node")
    if args.require_converged:
        non_converged = [record.name for record in records if not _matches_expected(record, args.expected_version, args.expected_commit)]
        if non_converged:
            raise SystemExit("hub transport cluster not converged: " + ", ".join(non_converged))

    print("\n".join(lines))
    print(
        f"hub transport cluster verify ok: nodes={len(records)} distinct_builds={len(distinct_builds)} "
        f"expected_consumers={len(expected_consumers)}"
    )
    return 0


def member_replace_plan(args: argparse.Namespace) -> int:
    print("Hub transport member replacement plan")
    print(f"1. Confirm the failed node is isolated: {args.failed_node}")
    print("2. Run hub transport precheck against the surviving hub nodes before introducing the replacement.")
    print(
        "3. Bring up the replacement node with the existing external cluster configuration "
        "(same cluster name, route mesh, JetStream domain, and operator-managed auth)."
    )
    if args.replacement_monitor_url:
        print(f"4. Verify replacement monitor endpoint: curl -fsS {args.replacement_monitor_url.rstrip('/')}/varz")
        print(f"5. Verify replacement route mesh: curl -fsS {args.replacement_monitor_url.rstrip('/')}/routez")
        print(
            "6. Verify replacement JetStream state: "
            f"curl -fsS '{args.replacement_monitor_url.rstrip('/')}/jsz?streams=true&consumers=true&config=true'"
        )
        next_step = 7
    else:
        next_step = 4
    print(
        f"{next_step}. Run hub transport cluster verify and confirm "
        f"{args.expected_stream} and controller-observed WORK_SITE consumers return to replicas={int(args.expected_replicas)}."
    )
    if args.controller_metrics_url:
        print(
            f"{next_step + 1}. Verify controller transport metrics recovered: "
            f"curl -fsS {args.controller_metrics_url.rstrip('/')} | "
            "rg 'ae_gateway_result_replay_backlog|ae_route_bundle_ack_age_seconds|ae_site_stale|ae_js_'"
        )
    if args.leaf_min_count is not None:
        print(
            f"{next_step + 2}. If hub leaves are in use, confirm leaf count recovered to at least {int(args.leaf_min_count)}."
        )
    print(
        "Non-goals: this helper does not generate NATS configs, perform auth rotation, "
        "or orchestrate remote restarts."
    )
    return 0


def _load_cluster_state(
    args: argparse.Namespace,
) -> tuple[list, str, list[str]]:
    if not args.node:
        raise SystemExit("at least one --node value is required")
    metrics_url = str(args.controller_metrics_url or "").strip()
    if not metrics_url:
        raise SystemExit("--controller-metrics-url is required")
    nodes = [parse_nats_hub_node_target(raw) for raw in args.node]
    timeout_s = float(args.timeout_seconds)
    records = [
        fetch_nats_hub_monitor_record(node, timeout_s=timeout_s, include_leafz=args.leaf_min_count is not None)
        for node in nodes
    ]
    metrics_text = fetch_http_text(metrics_url, timeout_s=timeout_s)
    expected_consumers = _expected_consumers_from_metrics(metrics_text, str(args.expected_stream))
    return records, metrics_text, expected_consumers


def _expected_consumers_from_metrics(metrics_text: str, expected_stream: str) -> list[str]:
    consumers: set[str] = set()
    for labels, _value in collect_prometheus_metric_values(metrics_text, "ae_js_consumer_pending"):
        stream = str(labels.get("stream") or "").strip()
        consumer = str(labels.get("consumer") or "").strip()
        if not consumer:
            continue
        if stream and stream != expected_stream:
            continue
        consumers.add(consumer)
    return sorted(consumers)


def _transport_metric_issues(
    metrics_text: str,
    *,
    backlog_threshold: float,
    ack_age_threshold: float,
) -> list[str]:
    issues: list[str] = []
    authority_samples = collect_prometheus_metric_values(metrics_text, "ae_controller_authority_healthy")
    if not authority_samples or max(value for _labels, value in authority_samples) < 1:
        issues.append("controller_authority_unhealthy")
    backlog_values = [
        value for _labels, value in collect_prometheus_metric_values(metrics_text, "ae_gateway_result_replay_backlog")
    ]
    if backlog_values and max(backlog_values) > backlog_threshold:
        issues.append(f"gateway_replay_backlog:{max(backlog_values)}/{backlog_threshold}")
    ack_age_values = [
        value for _labels, value in collect_prometheus_metric_values(metrics_text, "ae_route_bundle_ack_age_seconds")
    ]
    if ack_age_values and max(ack_age_values) > ack_age_threshold:
        issues.append(f"route_ack_age:{max(ack_age_values)}/{ack_age_threshold}")
    site_stale_values = [
        value for _labels, value in collect_prometheus_metric_values(metrics_text, "ae_site_stale")
    ]
    if any(value > 0 for value in site_stale_values):
        issues.append("site_stale")
    return issues


def _matches_expected(record, expected_version: str, expected_commit: str) -> bool:
    if str(record.version) != str(expected_version):
        return False
    if expected_commit:
        return str(record.git_commit) == str(expected_commit)
    return True


if __name__ == "__main__":
    raise SystemExit(main())
