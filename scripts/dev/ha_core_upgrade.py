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
    fetch_build_info,
    fetch_http_text,
    parse_ha_core_node_target,
    parse_nats_url,
    parse_prometheus_metric_value,
    read_etcd_leader,
    split_csv,
    tcp_connectable,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ha_core_upgrade.py",
        description="Plan and validate rolling upgrades for k1s-ha-core",
    )
    sub = parser.add_subparsers(dest="command_name", required=True)

    precheck = sub.add_parser(
        "precheck",
        help="Validate HA authority, transport health, and shared dependency reachability before upgrade",
    )
    precheck.add_argument("--metrics-url", default=os.getenv("HA_CORE_METRICS_URL") or "")
    precheck.add_argument("--etcd-endpoints", default=os.getenv("AE_ETCD_ENDPOINTS") or "")
    precheck.add_argument("--etcd-prefix", default=os.getenv("AE_ETCD_PREFIX") or "")
    precheck.add_argument("--nats-urls", default=os.getenv("AE_NATS_URL") or "")
    precheck.add_argument("--backlog-threshold", type=float, default=0.0)
    precheck.add_argument("--ack-age-threshold", type=float, default=15.0)
    precheck.add_argument("--timeout-seconds", type=float, default=3.0)
    precheck.add_argument("--dry-run", action="store_true")

    node_plan = sub.add_parser(
        "node-plan",
        help="Print the ordered rolling-upgrade steps for one core node",
    )
    node_plan.add_argument("--node-name", required=True)
    node_plan.add_argument(
        "--service",
        default=os.getenv("HA_CORE_SYSTEMD_SERVICE") or "ae-ha-core.service",
    )
    node_plan.add_argument(
        "--controller-url",
        default=os.getenv("HA_CORE_CONTROLLER_URL") or "http://127.0.0.1:9108",
    )
    node_plan.add_argument(
        "--apishim-url",
        default=os.getenv("HA_CORE_APISHIM_URL") or "https://127.0.0.1:8445",
    )
    node_plan.add_argument("--expected-version", default="")
    node_plan.add_argument("--expected-sha", default="")
    node_plan.add_argument("--leader", action="store_true")
    node_plan.add_argument(
        "--ingress-mode",
        choices=("disabled", "core-proxy", "core-to-edge-public"),
        default=os.getenv("EDGE_INGRESS_MODE") or "core-proxy",
    )

    verify = sub.add_parser(
        "cluster-verify",
        help="Verify build-window and authority health across all upgraded core nodes",
    )
    verify.add_argument(
        "--node",
        action="append",
        default=[],
        metavar="NAME=CONTROLLER_URL,APISHIM_URL",
        help="Node name and controller/apishim URLs; repeat once per core node",
    )
    verify.add_argument("--expected-version", required=True)
    verify.add_argument("--expected-sha", default="")
    verify.add_argument("--require-converged", action="store_true")
    verify.add_argument("--timeout-seconds", type=float, default=3.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command_name == "precheck":
        return precheck(args)
    if args.command_name == "node-plan":
        return node_plan(args)
    return cluster_verify(args)


def precheck(args: argparse.Namespace) -> int:
    if args.dry_run:
        print(
            f"DRY RUN precheck metrics_url={args.metrics_url or 'n/a'} "
            f"backlog_threshold={float(args.backlog_threshold)} "
            f"ack_age_threshold={float(args.ack_age_threshold)}"
        )
        return 0

    metrics_url = str(args.metrics_url or "").strip()
    endpoints = split_csv(args.etcd_endpoints)
    prefix = str(args.etcd_prefix or "").strip()
    nats_urls = split_csv(args.nats_urls)
    if not metrics_url:
        raise SystemExit("--metrics-url is required")
    if not endpoints or not prefix:
        raise SystemExit("--etcd-endpoints and --etcd-prefix are required")
    if not nats_urls:
        raise SystemExit("--nats-urls is required")

    leader = read_etcd_leader(endpoints, prefix, timeout_s=float(args.timeout_seconds))
    if leader is None:
        raise SystemExit("no controller leader record found")

    metrics = fetch_http_text(metrics_url, timeout_s=float(args.timeout_seconds))
    authority_healthy = parse_prometheus_metric_value(metrics, "ae_controller_authority_healthy")
    if authority_healthy is None or authority_healthy < 1:
        raise SystemExit("controller authority is not healthy")

    reachable_nats = 0
    for item in nats_urls:
        host, port = parse_nats_url(item)
        ok, _detail = tcp_connectable(host, port, timeout_s=float(args.timeout_seconds))
        if ok:
            reachable_nats += 1
    if reachable_nats == 0:
        raise SystemExit("no reachable NATS endpoints")

    backlog_values = [value for _labels, value in collect_prometheus_metric_values(metrics, "ae_gateway_result_replay_backlog")]
    if backlog_values and max(backlog_values) > float(args.backlog_threshold):
        raise SystemExit(
            f"gateway replay backlog exceeds threshold: max={max(backlog_values)} threshold={float(args.backlog_threshold)}"
        )

    ack_age_values = [value for _labels, value in collect_prometheus_metric_values(metrics, "ae_route_bundle_ack_age_seconds")]
    if ack_age_values and max(ack_age_values) > float(args.ack_age_threshold):
        raise SystemExit(
            f"route ack age exceeds threshold: max={max(ack_age_values)} threshold={float(args.ack_age_threshold)}"
        )

    stale_values = [value for _labels, value in collect_prometheus_metric_values(metrics, "ae_site_stale")]
    if any(value > 0 for value in stale_values):
        raise SystemExit("one or more sites are stale")

    backlog_max = max(backlog_values) if backlog_values else 0.0
    ack_age_max = max(ack_age_values) if ack_age_values else 0.0
    print(
        "precheck ok: "
        f"leader={leader.controller_id}:{leader.controller_epoch} "
        f"nats_reachable={reachable_nats} "
        f"backlog_max={backlog_max} "
        f"ack_age_max={ack_age_max}"
    )
    return 0


def node_plan(args: argparse.Namespace) -> int:
    role = "leader" if bool(args.leader) else "follower"
    print(f"Rolling upgrade plan for node {args.node_name} ({role})")
    print("1. Run cluster precheck and confirm authority/transport are healthy.")
    if args.leader:
        print("2. Confirm all follower core nodes already report the target build before touching the leader.")
    else:
        print("2. Confirm this node is not the elected leader before restart.")
    print(f"3. Stop the node service: systemctl stop {args.service}")
    print("4. Install the target build using the node's package or image delivery path.")
    print(f"5. Start the node service: systemctl start {args.service}")
    print(f"6. Verify controller build: curl -fsS {args.controller_url.rstrip('/')}/__ae/version")
    print(f"7. Verify apishim build: curl -fsSk {args.apishim_url.rstrip('/')}/__ae/version")
    print(
        "8. Verify controller authority/build metrics: "
        f"curl -fsS {args.controller_url.rstrip('/')}/metrics | rg 'ae_controller_authority_healthy|ae_controller_build_info'"
    )
    if args.ingress_mode == "core-proxy":
        print("9. Verify core-proxy listeners: ss -ltn | rg ':10080|:10443|:18080|:2333'")
    elif args.ingress_mode == "core-to-edge-public":
        print("9. Verify ingress listeners: ss -ltn | rg ':10080|:10443'")
    if args.expected_version or args.expected_sha:
        print(
            f"Expected build: version={args.expected_version or 'n/a'} "
            f"sha={args.expected_sha or 'n/a'}"
        )
    return 0


def cluster_verify(args: argparse.Namespace) -> int:
    if not args.node:
        raise SystemExit("at least one --node value is required")
    nodes = [parse_ha_core_node_target(raw) for raw in args.node]
    timeout_s = float(args.timeout_seconds)
    distinct_builds: set[tuple[str, str]] = set()
    target_seen = False
    lines: list[str] = []

    for node in nodes:
        controller = fetch_build_info(node.controller_url, timeout_s=timeout_s)
        apishim = fetch_build_info(node.apishim_url, timeout_s=timeout_s)
        if controller.component != "controller":
            raise SystemExit(f"{node.name}: unexpected controller component {controller.component!r}")
        if apishim.component != "apishim":
            raise SystemExit(f"{node.name}: unexpected apishim component {apishim.component!r}")
        if controller.version != apishim.version or controller.sha != apishim.sha:
            raise SystemExit(
                f"{node.name}: controller/apishim build mismatch "
                f"{controller.version}:{controller.sha} != {apishim.version}:{apishim.sha}"
            )

        metrics = fetch_http_text(f"{node.controller_url.rstrip('/')}/metrics", timeout_s=timeout_s)
        authority_healthy = parse_prometheus_metric_value(metrics, "ae_controller_authority_healthy")
        if authority_healthy is None or authority_healthy < 1:
            raise SystemExit(f"{node.name}: controller authority is not healthy")

        build_key = (controller.version, controller.sha)
        distinct_builds.add(build_key)
        if _matches_expected(controller, args.expected_version, args.expected_sha):
            target_seen = True
        lines.append(
            f"{node.name}: version={controller.version} sha={controller.sha} authority_healthy={int(authority_healthy)}"
        )

    if len(distinct_builds) > 2:
        raise SystemExit(f"upgrade window exceeded: found {len(distinct_builds)} distinct builds")
    if not target_seen:
        raise SystemExit("target build is not present on any node")
    if args.require_converged:
        non_converged = [
            node.name
            for node in nodes
            if not _matches_expected(
                fetch_build_info(node.controller_url, timeout_s=timeout_s),
                args.expected_version,
                args.expected_sha,
            )
        ]
        if non_converged:
            raise SystemExit(f"cluster not converged to target build: {', '.join(non_converged)}")

    print("\n".join(lines))
    print(f"cluster verify ok: nodes={len(nodes)} distinct_builds={len(distinct_builds)}")
    return 0


def _matches_expected(build, expected_version: str, expected_sha: str) -> bool:
    if str(build.version) != str(expected_version):
        return False
    if expected_sha:
        return str(build.sha) == str(expected_sha)
    return True


if __name__ == "__main__":
    raise SystemExit(main())
