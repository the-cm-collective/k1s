#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import shlex
import sys
from pathlib import Path

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from ae.ha.ops import (  # noqa: E402
    EtcdRestoreMemberSpec,
    build_container_etcdctl_command,
    build_local_etcdctl_recovery_command,
    build_quorum_restore_plan,
    derive_client_url,
    detect_container_cli_or_die,
    format_quorum_restore_plan,
    parse_etcd_member_add_output,
    required_parent_mounts,
    resolve_etcdctl_runner,
    split_csv,
    subprocess_run,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="etcd_recovery.py",
        description="Member replacement and quorum-restore helpers for HA k1s control planes",
    )
    parser.add_argument(
        "--runner",
        choices=("auto", "local", "container"),
        default="auto",
        help="Execution mode for etcdctl-backed commands",
    )
    parser.add_argument(
        "--print-command",
        action="store_true",
        help="Print the resolved etcdctl command instead of executing it",
    )
    parser.add_argument(
        "--etcdctl-bin",
        default=os.getenv("ETCDCTL_BIN", "etcdctl"),
        help="Path or name of local etcdctl binary",
    )
    parser.add_argument(
        "--image",
        default=os.getenv("AE_ETCD_SNAPSHOT_IMAGE", "quay.io/coreos/etcd:v3.5.13"),
        help="Container image used when --runner=container or local etcdctl is unavailable",
    )
    parser.add_argument(
        "--endpoints",
        default=os.getenv("AE_ETCD_ENDPOINTS", ""),
        help="Comma-separated etcd endpoints",
    )
    parser.add_argument("--cacert", default=os.getenv("AE_ETCD_CA") or None)
    parser.add_argument("--cert", default=os.getenv("AE_ETCD_CERT") or None)
    parser.add_argument("--key", default=os.getenv("AE_ETCD_KEY") or None)
    parser.add_argument("--user", default=os.getenv("AE_ETCD_USER") or None)
    parser.add_argument("--password", default=os.getenv("AE_ETCD_PASSWORD") or None)
    sub = parser.add_subparsers(dest="command", required=True)

    endpoint_status = sub.add_parser(
        "endpoint-status",
        help="Show etcd endpoint status for the configured cluster",
    )
    endpoint_status.add_argument(
        "--cluster",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Include the full cluster in endpoint status",
    )

    sub.add_parser("member-list", help="Show current etcd members")

    member_remove = sub.add_parser("member-remove", help="Remove a failed etcd member")
    member_remove.add_argument("--member-id", required=True, help="Member ID to remove")

    member_add = sub.add_parser("member-add", help="Add a replacement etcd learner member")
    member_add.add_argument("--name", required=True, help="Name for the replacement member")
    member_add.add_argument("--peer-url", required=True, help="Peer URL for the replacement member")

    member_promote = sub.add_parser("member-promote", help="Promote a caught-up learner")
    member_promote.add_argument("--member-id", required=True, help="Learner member ID to promote")

    quorum_plan = sub.add_parser(
        "quorum-restore-plan",
        help="Print the restore and start commands for a fresh 3-member cluster from one snapshot",
    )
    quorum_plan.add_argument("--input", required=True, help="Snapshot input path")
    quorum_plan.add_argument(
        "--cluster-token",
        required=True,
        help="Initial cluster token for the restored three-member cluster",
    )
    quorum_plan.add_argument(
        "--member",
        action="append",
        default=[],
        metavar="NAME=PEER_URL",
        help="Restored member name and peer URL; provide exactly three",
    )
    quorum_plan.add_argument(
        "--data-dir",
        default="/var/lib/etcd",
        help="Data dir to use on each restored member",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "quorum-restore-plan":
        return _quorum_restore_plan(args)

    endpoints = split_csv(args.endpoints)
    if not endpoints:
        raise SystemExit("AE_ETCD_ENDPOINTS or --endpoints is required")
    try:
        runner = resolve_etcdctl_runner(args.runner, args.etcdctl_bin)
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc

    cmd, extra_env = build_local_etcdctl_recovery_command(
        args.command,
        endpoints=endpoints,
        member_id=getattr(args, "member_id", None),
        member_name=getattr(args, "name", None),
        peer_urls=getattr(args, "peer_url", None),
        cluster=bool(getattr(args, "cluster", False)),
        binary=(args.etcdctl_bin if runner == "local" else "etcdctl"),
        ca_cert=args.cacert,
        cert=args.cert,
        key=args.key,
        user=args.user,
        password=args.password,
    )
    resolved_cmd = _wrap_container_if_needed(cmd, runner=runner, args=args, extra_env=extra_env)
    if args.print_command:
        print(shlex.join(resolved_cmd))
        return 0

    if args.command == "member-add":
        proc = subprocess_run(
            resolved_cmd,
            env=(extra_env if runner == "local" else None),
            capture_output=True,
        )
        stdout = str(proc.stdout or "").strip()
        stderr = str(proc.stderr or "").strip()
        if stderr:
            print(stderr, file=sys.stderr)
        if stdout:
            print(stdout)
        result = parse_etcd_member_add_output(
            stdout,
            expected_name=args.name,
            expected_peer_urls=args.peer_url,
        )
        print("")
        print("Replacement member settings:")
        print(f'  ETCD_NAME="{result.member_name}"')
        print(f'  ETCD_INITIAL_CLUSTER="{result.initial_cluster}"')
        print(f'  ETCD_INITIAL_CLUSTER_STATE="{result.initial_cluster_state}"')
        if result.initial_advertise_peer_urls:
            print(
                f'  ETCD_INITIAL_ADVERTISE_PEER_URLS="{result.initial_advertise_peer_urls}"'
            )
        if result.member_id:
            print(f"  member_id={result.member_id}")
        return 0

    subprocess_run(resolved_cmd, env=(extra_env if runner == "local" else None))
    return 0


def _wrap_container_if_needed(
    cmd: list[str],
    *,
    runner: str,
    args: argparse.Namespace,
    extra_env: dict[str, str],
) -> list[str]:
    if runner == "local":
        return cmd
    return build_container_etcdctl_command(
        detect_container_cli_or_die(),
        args.image,
        cmd,
        mounts=required_parent_mounts([args.cacert, args.cert, args.key]),
        extra_env=extra_env,
    )


def _quorum_restore_plan(args: argparse.Namespace) -> int:
    member_specs = [_parse_member_spec(raw, default_data_dir=args.data_dir) for raw in args.member]
    plan = build_quorum_restore_plan(
        snapshot_path=Path(args.input).expanduser().resolve(),
        cluster_token=str(args.cluster_token),
        members=member_specs,
        binary=args.etcdctl_bin,
    )
    print(format_quorum_restore_plan(plan))
    return 0


def _parse_member_spec(raw: str, *, default_data_dir: str) -> EtcdRestoreMemberSpec:
    text = str(raw or "").strip()
    if "=" not in text:
        raise SystemExit(f"invalid --member value {raw!r}; expected NAME=PEER_URL")
    name, peer_url = text.split("=", 1)
    member_name = name.strip()
    normalized_peer_url = peer_url.strip()
    if not member_name or not normalized_peer_url:
        raise SystemExit(f"invalid --member value {raw!r}; expected NAME=PEER_URL")
    try:
        client_url = derive_client_url(normalized_peer_url)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    return EtcdRestoreMemberSpec(
        name=member_name,
        peer_url=normalized_peer_url,
        client_url=client_url,
        data_dir=str(default_data_dir),
    )


if __name__ == "__main__":
    raise SystemExit(main())
