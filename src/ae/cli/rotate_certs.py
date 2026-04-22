"""CLI helper to issue node certs and join tokens."""

from __future__ import annotations

import argparse
import os
from datetime import datetime, timedelta

from ae._utc import UTC
from ae.security import issue_cert, issue_token


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ae.rotate-certs", description="Issue a new agent cert for a node"
    )
    parser.add_argument("--node-id", default=os.getenv("AE_NODE_ID"))
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument("--join-secret", default=os.getenv("AE_AGENT_JOIN_SECRET"))
    parser.add_argument("--root", default=os.getenv("AE_TLS_DIR", "state/tls"))
    args = parser.parse_args(argv)

    if not args.node_id:
        print("node id required (flag or AE_NODE_ID)")
        return 1
    if not args.join_secret:
        print("join secret required (AE_AGENT_JOIN_SECRET)")
        return 1
    exp = datetime.now(UTC) + timedelta(days=args.days)
    token = issue_token(args.node_id, exp, secret=args.join_secret)
    crt, key, ca = issue_cert(args.node_id, root=args.root, days=args.days)
    print("issued new cert")
    print("cert:", crt)
    print("key:", key)
    print("ca:", ca)
    print("join_token:", token)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
