"""Site Gateway entry point (Phase 2 skeleton)."""

from __future__ import annotations

import argparse
import os

from ae.config.transport import GatewayJetStreamConfig, TransportConfig
from ae.controller.node_identity import scoped_node_id
from ae.gateway.service import SiteGateway
from ae.observability.logging import configure_logging
from ae.transport.nats_client import NatsClient, NatsClientError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ae.gateway",
        description="k1s site gateway (phase 2 skeleton)",
    )
    parser.add_argument("--site-id", default=os.getenv("AE_SITE_ID"))
    parser.add_argument("--node-id", default=os.getenv("AE_NODE_ID"))
    parser.add_argument("--nats-url", default=os.getenv("AE_NATS_URL"))
    parser.add_argument("--log-level", default=None)
    parser.add_argument("--once", action="store_true", help="Run checks and exit")
    parser.add_argument(
        "--status-interval",
        type=int,
        default=int(os.getenv("AE_GATEWAY_STATUS_INTERVAL", "30") or 30),
        help="Seconds between status logs",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.log_level:
        configure_logging(args.log_level.upper())
    else:
        configure_logging(None)

    transport = TransportConfig.from_env()
    if transport.backend == "http":
        # Gateway is only meaningful for NATS transport; warn but allow dry-run.
        import logging

        logging.getLogger(__name__).warning(
            "AE_TRANSPORT_BACKEND=%s; gateway is intended for NATS transport",
            transport.backend,
        )
    else:
        import logging

        logging.getLogger(__name__).info(
            "AE_TRANSPORT_BACKEND=%s; starting NATS gateway",
            transport.backend,
        )

    if not args.site_id:
        raise SystemExit("AE_SITE_ID or --site-id is required")

    node_id = args.node_id
    if node_id:
        node_id = scoped_node_id(args.site_id, str(node_id))

    nats_client = None
    if transport.backend in {"nats-core", "nats-js"}:
        if not args.nats_url:
            raise SystemExit("AE_NATS_URL or --nats-url required for NATS transport")
        try:
            nats_client = NatsClient(
                url=args.nats_url,
                creds=transport.nats_creds,
                name="k1s-gateway",
            )
        except NatsClientError as exc:
            raise SystemExit(str(exc)) from exc

    js_config = GatewayJetStreamConfig.from_env()
    gateway = SiteGateway(
        site_id=args.site_id,
        node_id=node_id,
        nats_url=args.nats_url,
        js_config=js_config,
        status_interval_s=args.status_interval,
        nats_client=nats_client,
    )
    gateway.start(once=args.once)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
