"""Stub worker that executes local work and publishes results."""

from __future__ import annotations

import argparse
import json
import os
import threading
import time
from datetime import datetime, timezone

from ae.observability.logging import configure_logging
from ae.transport import local_result_subject, local_work_progress_subject, local_work_subject
from ae.transport.nats_client import NatsClient, NatsClientError, NatsMessage


class WorkerStub:
    def __init__(
        self,
        *,
        node_id: str,
        nats_url: str,
        delay_ms: int,
        status: str,
        progress_interval_s: float,
    ) -> None:
        self._node_id = node_id
        self._nats_url = nats_url
        self._delay_ms = max(0, int(delay_ms))
        self._status = status
        self._progress_interval_s = max(0.0, float(progress_interval_s))
        self._client = NatsClient(url=nats_url, name=f"k1s-worker-{node_id}")

    def start(self) -> None:
        self._client.connect()
        self._client.subscribe(local_work_subject(self._node_id), self._on_work)
        while True:
            time.sleep(1)

    def _on_work(self, msg: NatsMessage) -> None:
        payload = _safe_json(msg.data)
        thread = threading.Thread(target=self._run_work, args=(payload,), daemon=True)
        thread.start()

    def _run_work(self, payload: dict) -> None:
        start_ts = datetime.now(timezone.utc)
        if self._delay_ms:
            self._run_with_progress(payload)
        finish_ts = datetime.now(timezone.utc)
        work_id = payload.get("work_id") or "unknown"
        attempt = payload.get("attempt") or 0
        site_id = payload.get("site_id")
        desired_gen = payload.get("desired_generation")
        result = {
            "work_id": work_id,
            "attempt": attempt,
            "site_id": site_id,
            "node_id": self._node_id,
            "status": self._status,
            "reason": "stub",
            "observed_generation": desired_gen,
            "started_at": start_ts.isoformat(),
            "finished_at": finish_ts.isoformat(),
            "outputs": {"echo": payload},
        }
        self._client.publish_json(local_result_subject(), result)

    def _run_with_progress(self, payload: dict) -> None:
        end_at = time.monotonic() + (self._delay_ms / 1000.0)
        if self._progress_interval_s <= 0:
            time.sleep(self._delay_ms / 1000.0)
            return
        next_progress = time.monotonic()
        while True:
            now = time.monotonic()
            if now >= next_progress:
                self._publish_progress(payload)
                next_progress = now + self._progress_interval_s
            remaining = end_at - now
            if remaining <= 0:
                break
            time.sleep(min(0.5, remaining))

    def _publish_progress(self, payload: dict) -> None:
        work_id = payload.get("work_id")
        attempt = payload.get("attempt")
        if work_id is None or attempt is None:
            return
        progress = {
            "work_id": work_id,
            "attempt": attempt,
            "node_id": self._node_id,
            "status": "running",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        try:
            self._client.publish_json(local_work_progress_subject(), progress)
        except Exception:
            pass

def _safe_json(payload: bytes) -> dict:
    try:
        if not payload:
            return {}
        return json.loads(payload.decode("utf-8"))
    except Exception:
        return {}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ae.worker-stub",
        description="Stub worker for NATS gateway testing",
    )
    parser.add_argument("--node-id", default="worker-1")
    parser.add_argument("--nats-url", default="nats://127.0.0.1:4223")
    parser.add_argument("--delay-ms", type=int, default=50)
    parser.add_argument(
        "--progress-interval",
        type=float,
        default=float(os.getenv("AE_WORKER_PROGRESS_INTERVAL", "5") or 5),
        help="Seconds between work progress heartbeats",
    )
    parser.add_argument("--status", default="succeeded")
    parser.add_argument("--log-level", default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.log_level:
        configure_logging(args.log_level.upper())
    else:
        configure_logging(None)
    try:
        worker = WorkerStub(
            node_id=args.node_id,
            nats_url=args.nats_url,
            delay_ms=args.delay_ms,
            status=args.status,
            progress_interval_s=args.progress_interval,
        )
        worker.start()
    except NatsClientError as exc:
        raise SystemExit(str(exc)) from exc
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
