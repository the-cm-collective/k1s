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
    build_container_etcdctl_command,
    build_local_etcdctl_command,
    detect_container_cli_or_die,
    required_parent_mounts,
    resolve_etcdctl_runner,
    split_csv,
    subprocess_run,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="etcd_snapshot.py",
        description="Create and restore etcd snapshots for HA k1s control planes",
    )
    parser.add_argument(
        "--runner",
        choices=("auto", "local", "container"),
        default="auto",
        help="Execution mode for etcdctl",
    )
    parser.add_argument(
        "--print-command",
        action="store_true",
        help="Print the resolved command instead of executing it",
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

    save = sub.add_parser("save", help="Save a live etcd snapshot")
    save.add_argument("--output", required=True, help="Snapshot output path")

    status = sub.add_parser("status", help="Show snapshot metadata")
    status.add_argument("--input", required=True, help="Snapshot input path")

    restore = sub.add_parser("restore", help="Restore a snapshot into a new data dir")
    restore.add_argument("--input", required=True, help="Snapshot input path")
    restore.add_argument("--data-dir", required=True, help="Target restore data dir")
    restore.add_argument("--name", default=None, help="Optional member name")
    restore.add_argument("--initial-cluster", default=None, help="Optional initial-cluster value")
    restore.add_argument(
        "--initial-advertise-peer-urls",
        default=None,
        help="Optional initial advertise peer URLs",
    )
    restore.add_argument(
        "--initial-cluster-token",
        default=None,
        help="Optional initial cluster token",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    endpoints = split_csv(args.endpoints)
    if args.command == "save" and not endpoints:
        raise SystemExit("AE_ETCD_ENDPOINTS or --endpoints is required for snapshot save")

    snapshot_path = None
    data_dir = None
    if args.command == "save":
        snapshot_path = Path(args.output).expanduser().resolve()
    elif args.command in {"status", "restore"}:
        snapshot_path = Path(args.input).expanduser().resolve()
    if args.command == "restore":
        data_dir = Path(args.data_dir).expanduser().resolve()

    local_cmd, extra_env = build_local_etcdctl_command(
        args.command,
        endpoints=endpoints or None,
        snapshot_path=snapshot_path,
        data_dir=data_dir,
        name=getattr(args, "name", None),
        initial_cluster=getattr(args, "initial_cluster", None),
        initial_advertise_peer_urls=getattr(args, "initial_advertise_peer_urls", None),
        initial_cluster_token=getattr(args, "initial_cluster_token", None),
        binary=args.etcdctl_bin,
        ca_cert=args.cacert,
        cert=args.cert,
        key=args.key,
        user=args.user,
        password=args.password,
    )

    try:
        runner = resolve_etcdctl_runner(args.runner, args.etcdctl_bin)
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc
    if runner == "local":
        cmd = local_cmd
    else:
        container_cmd, container_env = build_local_etcdctl_command(
            args.command,
            endpoints=endpoints or None,
            snapshot_path=snapshot_path,
            data_dir=data_dir,
            name=getattr(args, "name", None),
            initial_cluster=getattr(args, "initial_cluster", None),
            initial_advertise_peer_urls=getattr(args, "initial_advertise_peer_urls", None),
            initial_cluster_token=getattr(args, "initial_cluster_token", None),
            binary="etcdctl",
            ca_cert=args.cacert,
            cert=args.cert,
            key=args.key,
            user=args.user,
            password=args.password,
        )
        cmd = build_container_etcdctl_command(
            detect_container_cli_or_die(),
            args.image,
            container_cmd,
            mounts=required_parent_mounts(
                [
                    snapshot_path,
                    data_dir,
                    args.cacert,
                    args.cert,
                    args.key,
                ]
            ),
            extra_env=container_env,
        )
        extra_env = {}

    if args.print_command:
        print(shlex.join(cmd))
        return 0
    subprocess_run(cmd, env=extra_env)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
