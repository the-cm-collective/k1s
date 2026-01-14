from __future__ import annotations

import argparse
import os
import sys

from .server import run_server


def cmd_serve(args: argparse.Namespace) -> int:
    token = args.token or os.getenv("AE_APISHIM_TOKEN")
    if os.getenv("AE_APISHIM_ENABLE") != "1":
        raise SystemExit("AE_APISHIM_ENABLE=1 required to run the API shim")
    run_server(host=args.host, port=args.port, token=token, tls=args.tls)
    return 0


def cmd_kubeconfig(args: argparse.Namespace) -> int:
    server = args.server.rstrip("/")
    token = args.token or os.getenv("AE_APISHIM_TOKEN", "changeme")
    context = args.context
    cluster = context
    user = context
    cfg = f"""
apiVersion: v1
kind: Config
clusters:
- name: {cluster}
  cluster:
    server: {server}
    insecure-skip-tls-verify: {str(args.insecure_skip_tls_verify).lower()}
contexts:
- name: {context}
  context:
    cluster: {cluster}
    user: {user}
current-context: {context}
users:
- name: {user}
  user:
    token: {token}
""".lstrip()
    sys.stdout.write(cfg)
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m ae.apishim", description="k1s Kubernetes API shim")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="Run the API shim server")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", default=8445, type=int)
    s.add_argument("--token", default=None, help="Bearer token (or AE_APISHIM_TOKEN env)")
    s.add_argument(
        "--tls",
        action="store_true",
        help="Enable TLS (requires AE_APISHIM_TLS_CERT/KEY)",
    )
    s.set_defaults(func=cmd_serve)

    k = sub.add_parser("kubeconfig", help="Emit a kubeconfig pointing to the shim")
    k.add_argument(
        "--server",
        required=True,
        help="Shim server URL, e.g. https://127.0.0.1:8445 or http://...",
    )
    k.add_argument("--token", default=None, help="Bearer token (or AE_APISHIM_TOKEN env)")
    k.add_argument("--context", default="k1s-apishim")
    k.add_argument("--insecure-skip-tls-verify", action="store_true")
    k.set_defaults(func=cmd_kubeconfig)

    args = p.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
