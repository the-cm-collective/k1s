#!/usr/bin/env python3
"""Deep ingress probes for websocket/lb/stickiness/perf validation."""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.client
import json
import os
import socket
import ssl
import statistics
import struct
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from http.cookies import SimpleCookie
from typing import Any
from urllib.parse import urlsplit

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


@dataclass
class Endpoint:
    scheme: str
    host: str
    port: int
    path: str


def _quantile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return float(values[0])
    try:
        return float(statistics.quantiles(values, n=100)[int(q * 100) - 1])
    except Exception:
        idx = int((len(values) - 1) * q)
        return float(sorted(values)[idx])


def _latency_summary(samples: list[float]) -> dict[str, float]:
    if not samples:
        return {
            "p50_ms": 0.0,
            "p95_ms": 0.0,
            "p99_ms": 0.0,
            "max_ms": 0.0,
            "mean_ms": 0.0,
        }
    return {
        "p50_ms": _quantile(samples, 0.50),
        "p95_ms": _quantile(samples, 0.95),
        "p99_ms": _quantile(samples, 0.99),
        "max_ms": float(max(samples)),
        "mean_ms": float(statistics.mean(samples)),
    }


def _parse_endpoint(url: str) -> Endpoint:
    parsed = urlsplit(url)
    scheme = (parsed.scheme or "").lower()
    if scheme not in {"http", "https", "ws", "wss"}:
        raise ValueError(f"unsupported scheme in url: {url}")
    host = parsed.hostname or ""
    if not host:
        raise ValueError(f"missing hostname in url: {url}")
    if parsed.port is not None:
        port = int(parsed.port)
    elif scheme in {"https", "wss"}:
        port = 443
    else:
        port = 80
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    return Endpoint(scheme=scheme, host=host, port=port, path=path)


def _http_request(
    endpoint: Endpoint,
    host_header: str,
    timeout: float,
    headers: dict[str, str] | None = None,
    method: str = "GET",
) -> tuple[int, dict[str, str], bytes, float]:
    extra_headers = dict(headers or {})
    extra_headers["Host"] = host_header

    started = time.perf_counter()
    conn: http.client.HTTPConnection | http.client.HTTPSConnection
    if endpoint.scheme in {"https", "wss"}:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        conn = http.client.HTTPSConnection(endpoint.host, endpoint.port, timeout=timeout, context=ctx)
    else:
        conn = http.client.HTTPConnection(endpoint.host, endpoint.port, timeout=timeout)
    try:
        conn.request(method, endpoint.path, headers=extra_headers)
        response = conn.getresponse()
        body = response.read()
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        header_map = {k.lower(): v for k, v in response.getheaders()}
        return int(response.status), header_map, body, elapsed_ms
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _extract_backend_id_with_source(
    body: bytes,
    headers: dict[str, str] | None = None,
) -> tuple[str, str]:
    header_map = headers or {}
    for key in (
        "x-k1s-edge-backend",
        "x-k1s-backend-id",
        "x-backend-id",
    ):
        value = str(header_map.get(key) or "").strip()
        if value:
            return value, "header"

    text = body.decode("utf-8", "replace").strip()
    if not text:
        return "empty", "body"
    try:
        payload = json.loads(text)
    except Exception:
        return f"raw:{hashlib.sha1(body).hexdigest()[:12]}", "body"  # noqa: S324

    for key in ("backend_id", "backend", "id", "hostname"):
        value = payload.get(key)
        if value:
            return str(value), "body"
    os_obj = payload.get("os")
    if isinstance(os_obj, dict) and os_obj.get("hostname"):
        return str(os_obj["hostname"]), "body"
    return f"json:{hashlib.sha1(text.encode('utf-8')).hexdigest()[:12]}", "body"  # noqa: S324


def _extract_backend_id(body: bytes, headers: dict[str, str] | None = None) -> str:
    backend_id, _source = _extract_backend_id_with_source(body, headers=headers)
    return backend_id


def _ws_connect(endpoint: Endpoint, host_header: str, timeout: float) -> socket.socket:
    raw = socket.create_connection((endpoint.host, endpoint.port), timeout=timeout)
    if endpoint.scheme in {"https", "wss"}:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        raw = ctx.wrap_socket(raw, server_hostname=endpoint.host)
    raw.settimeout(timeout)

    ws_key = base64.b64encode(os.urandom(16)).decode("ascii")
    request = (
        f"GET {endpoint.path} HTTP/1.1\r\n"
        f"Host: {host_header}\r\n"
        "Connection: Upgrade\r\n"
        "Upgrade: websocket\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        f"Sec-WebSocket-Key: {ws_key}\r\n"
        "\r\n"
    ).encode("ascii")
    raw.sendall(request)

    response = b""
    while b"\r\n\r\n" not in response and len(response) < 65536:
        chunk = raw.recv(4096)
        if not chunk:
            break
        response += chunk
    header_blob = response.split(b"\r\n\r\n", 1)[0].decode("utf-8", "replace")
    status_line = header_blob.splitlines()[0] if header_blob else ""
    if " 101 " not in f" {status_line} " and not status_line.startswith("HTTP/1.1 101"):
        raw.close()
        raise RuntimeError(f"websocket upgrade failed: {status_line or 'empty response'}")
    expected_accept = base64.b64encode(hashlib.sha1(f"{ws_key}{WS_GUID}".encode("ascii")).digest()).decode(  # noqa: S324
        "ascii"
    )
    lower_headers = header_blob.lower()
    if f"sec-websocket-accept: {expected_accept.lower()}" not in lower_headers:
        raw.close()
        raise RuntimeError("websocket upgrade invalid sec-websocket-accept")
    return raw


def _ws_send_text(sock: socket.socket, payload: bytes) -> None:
    # Client frames must be masked.
    first = 0x80 | 0x1
    length = len(payload)
    if length < 126:
        header = bytearray([first, 0x80 | length])
    elif length <= 0xFFFF:
        header = bytearray([first, 0x80 | 126]) + struct.pack("!H", length)
    else:
        header = bytearray([first, 0x80 | 127]) + struct.pack("!Q", length)
    mask = os.urandom(4)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    sock.sendall(bytes(header) + mask + masked)


def _ws_send_pong(sock: socket.socket, payload: bytes) -> None:
    first = 0x80 | 0xA
    length = len(payload)
    if length < 126:
        header = bytearray([first, 0x80 | length])
    elif length <= 0xFFFF:
        header = bytearray([first, 0x80 | 126]) + struct.pack("!H", length)
    else:
        header = bytearray([first, 0x80 | 127]) + struct.pack("!Q", length)
    mask = os.urandom(4)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    sock.sendall(bytes(header) + mask + masked)


def _sock_recv_exact(sock: socket.socket, length: int) -> bytes:
    data = bytearray()
    while len(data) < length:
        chunk = sock.recv(length - len(data))
        if not chunk:
            raise RuntimeError("unexpected websocket EOF")
        data.extend(chunk)
    return bytes(data)


def _ws_recv_frame(sock: socket.socket) -> tuple[int, bytes]:
    hdr = _sock_recv_exact(sock, 2)
    b0, b1 = hdr
    opcode = b0 & 0x0F
    masked = (b1 & 0x80) != 0
    length = b1 & 0x7F
    if length == 126:
        ext = _sock_recv_exact(sock, 2)
        length = struct.unpack("!H", ext)[0]
    elif length == 127:
        ext = _sock_recv_exact(sock, 8)
        length = struct.unpack("!Q", ext)[0]
    mask_key = b""
    if masked:
        mask_key = _sock_recv_exact(sock, 4)
    payload = bytearray(_sock_recv_exact(sock, length))
    if masked:
        payload = bytearray(b ^ mask_key[i % 4] for i, b in enumerate(payload))
    return opcode, bytes(payload)


def run_ws_soak(args: argparse.Namespace) -> int:
    endpoint = _parse_endpoint(args.url)
    if endpoint.scheme == "http":
        endpoint.scheme = "ws"
    if endpoint.scheme == "https":
        endpoint.scheme = "wss"
    deadline = time.time() + float(args.duration_seconds)
    heartbeat = max(0.1, float(args.heartbeat_seconds))

    lock = threading.Lock()
    connected = 0
    connect_failures = 0
    disconnects = 0
    messages_sent = 0
    messages_recv = 0
    rtt_ms: list[float] = []

    def worker(worker_id: int) -> None:
        nonlocal connected, connect_failures, disconnects, messages_sent, messages_recv
        while time.time() < deadline:
            sock: socket.socket | None = None
            try:
                sock = _ws_connect(endpoint, args.host, timeout=float(args.timeout_seconds))
                with lock:
                    connected += 1
                while time.time() < deadline:
                    sent_at = time.perf_counter()
                    payload = f"probe:{worker_id}:{int(sent_at * 1000)}".encode("utf-8")
                    _ws_send_text(sock, payload)
                    with lock:
                        messages_sent += 1
                    opcode, frame_payload = _ws_recv_frame(sock)
                    if opcode == 0x9:
                        _ws_send_pong(sock, frame_payload)
                        continue
                    if opcode == 0x8:
                        with lock:
                            disconnects += 1
                        break
                    if opcode == 0x1:
                        with lock:
                            messages_recv += 1
                            rtt_ms.append((time.perf_counter() - sent_at) * 1000.0)
                    time.sleep(heartbeat)
            except Exception:
                with lock:
                    connect_failures += 1
                    disconnects += 1
                time.sleep(0.2)
            finally:
                if sock is not None:
                    try:
                        sock.close()
                    except Exception:
                        pass

    workers = max(1, int(args.connections))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for idx in range(workers):
            pool.submit(worker, idx)

    attempted = connected + connect_failures
    connected_ratio = (connected / attempted) if attempted else 0.0
    elapsed = float(args.duration_seconds)
    payload = {
        "probe": "ws_soak",
        "duration_s": elapsed,
        "connections_requested": workers,
        "attempted_connections": attempted,
        "connected": connected,
        "connect_failures": connect_failures,
        "connected_ratio": connected_ratio,
        "disconnects": disconnects,
        "messages_sent": messages_sent,
        "messages_recv": messages_recv,
        "message_loss": max(0, messages_sent - messages_recv),
        "messages_per_second_sent": (messages_sent / elapsed) if elapsed > 0 else 0.0,
        "messages_per_second_recv": (messages_recv / elapsed) if elapsed > 0 else 0.0,
        "rtt": _latency_summary(rtt_ms),
    }
    ok = connected > 0 and messages_recv > 0 and connected_ratio >= float(args.min_connected_ratio)
    payload["pass"] = bool(ok)
    print(json.dumps(payload, separators=(",", ":"), sort_keys=True))
    return 0 if ok else 2


def run_lb_sample(args: argparse.Namespace) -> int:
    endpoint = _parse_endpoint(args.url)
    requests = max(1, int(args.requests))
    timeout = float(args.timeout_seconds)
    latencies: list[float] = []
    backends: Counter[str] = Counter()
    codes: Counter[int] = Counter()
    errors = 0
    backend_header_hits = 0
    backend_body_hits = 0
    started = time.perf_counter()

    for _ in range(requests):
        try:
            code, _headers, body, elapsed_ms = _http_request(endpoint, args.host, timeout=timeout)
            codes[code] += 1
            latencies.append(elapsed_ms)
            if 200 <= code < 400:
                backend_id, source = _extract_backend_id_with_source(body, headers=_headers)
                if source == "header":
                    backend_header_hits += 1
                else:
                    backend_body_hits += 1
                backends[backend_id] += 1
            else:
                errors += 1
        except Exception:
            errors += 1

    elapsed_s = max(0.001, time.perf_counter() - started)
    backend_counts = dict(backends)
    observed = sum(backend_counts.values())
    distribution_ok = False
    max_skew = 1.0
    if len(backend_counts) >= 2 and observed > 0:
        ideal = observed / float(len(backend_counts))
        deltas = [abs(v - ideal) / ideal for v in backend_counts.values()]
        max_skew = max(deltas) if deltas else 1.0
        distribution_ok = max_skew <= float(args.max_skew_ratio)

    payload = {
        "probe": "lb_sample",
        "strategy": args.strategy,
        "requests": requests,
        "responses": observed,
        "errors": errors,
        "error_rate": float(errors) / float(requests),
        "codes": {str(k): v for k, v in codes.items()},
        "counts_by_backend": backend_counts,
        "backend_count": len(backend_counts),
        "distribution_ok": distribution_ok,
        "max_skew_ratio": max_skew,
        "backend_identity": {
            "header_name": "x-k1s-edge-backend",
            "header_hits": backend_header_hits,
            "body_hits": backend_body_hits,
        },
        "latency": _latency_summary(latencies),
        "rps": float(requests) / elapsed_s,
    }
    ok = errors == 0 and len(backend_counts) >= int(args.min_backends)
    if args.require_distribution:
        ok = ok and distribution_ok
    payload["pass"] = bool(ok)
    print(json.dumps(payload, separators=(",", ":"), sort_keys=True))
    return 0 if ok else 2


def run_sticky_probe(args: argparse.Namespace) -> int:
    endpoint = _parse_endpoint(args.url)
    clients = max(1, int(args.clients))
    requests_per_client = max(1, int(args.requests_per_client))
    timeout = float(args.timeout_seconds)
    min_pin_ratio = float(args.min_pin_ratio)
    latencies: list[float] = []
    all_ok = True
    client_payloads: list[dict[str, Any]] = []
    codes: Counter[int] = Counter()

    for client_id in range(clients):
        cookies: dict[str, str] = {}
        counts: Counter[str] = Counter()
        errors = 0
        for _ in range(requests_per_client):
            headers: dict[str, str] = {}
            if cookies:
                headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in sorted(cookies.items()))
            try:
                code, header_map, body, elapsed_ms = _http_request(
                    endpoint, args.host, timeout=timeout, headers=headers
                )
                latencies.append(elapsed_ms)
                codes[code] += 1
                raw_set_cookie = header_map.get("set-cookie", "")
                if raw_set_cookie:
                    parsed = SimpleCookie()
                    parsed.load(raw_set_cookie)
                    for morsel in parsed.values():
                        cookies[morsel.key] = morsel.value
                if 200 <= code < 400:
                    counts[_extract_backend_id(body)] += 1
                else:
                    errors += 1
            except Exception:
                errors += 1
        total = sum(counts.values())
        primary_backend = ""
        primary_count = 0
        if counts:
            primary_backend, primary_count = counts.most_common(1)[0]
        pin_ratio = float(primary_count) / float(total) if total else 0.0
        client_ok = errors == 0 and total > 0 and pin_ratio >= min_pin_ratio
        all_ok = all_ok and client_ok
        client_payloads.append(
            {
                "client_id": client_id,
                "counts_by_backend": dict(counts),
                "primary_backend": primary_backend,
                "pin_ratio": pin_ratio,
                "cookies_seen": sorted(cookies.keys()),
                "errors": errors,
                "pass": client_ok,
            }
        )

    payload = {
        "probe": "sticky_probe",
        "clients": clients,
        "requests_per_client": requests_per_client,
        "codes": {str(k): v for k, v in codes.items()},
        "client_results": client_payloads,
        "pin_ok": all_ok,
        "latency": _latency_summary(latencies),
        "rebind_on_backend_loss_ok": None,
        "pass": all_ok,
    }
    print(json.dumps(payload, separators=(",", ":"), sort_keys=True))
    return 0 if all_ok else 2


def run_http_bench(args: argparse.Namespace) -> int:
    endpoint = _parse_endpoint(args.url)
    duration_s = max(1.0, float(args.duration_seconds))
    concurrency = max(1, int(args.concurrency))
    warmup_s = max(0.0, float(args.warmup_seconds))
    deadline = time.time() + duration_s
    warmup_deadline = time.time() + warmup_s
    timeout = float(args.timeout_seconds)

    lock = threading.Lock()
    codes: Counter[int] = Counter()
    errors = 0
    bytes_recv = 0
    latencies: list[float] = []
    started = time.perf_counter()

    def worker() -> None:
        nonlocal errors, bytes_recv
        local_lat: list[float] = []
        local_codes: Counter[int] = Counter()
        local_errors = 0
        local_bytes = 0
        while time.time() < deadline:
            try:
                code, _headers, body, elapsed_ms = _http_request(endpoint, args.host, timeout=timeout)
                if time.time() >= warmup_deadline:
                    local_lat.append(elapsed_ms)
                    local_codes[code] += 1
                    local_bytes += len(body)
            except Exception:
                if time.time() >= warmup_deadline:
                    local_errors += 1
        with lock:
            latencies.extend(local_lat)
            codes.update(local_codes)
            errors += local_errors
            bytes_recv += local_bytes

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        for _ in range(concurrency):
            pool.submit(worker)

    elapsed = max(0.001, time.perf_counter() - started)
    completed = int(sum(codes.values()))
    total = completed + errors
    payload = {
        "probe": "http_bench",
        "duration_s": duration_s,
        "warmup_s": warmup_s,
        "concurrency": concurrency,
        "completed_requests": completed,
        "errors": errors,
        "error_rate": float(errors) / float(total) if total else 0.0,
        "codes": {str(k): v for k, v in codes.items()},
        "rps": float(completed) / elapsed,
        "bytes_per_second": float(bytes_recv) / elapsed,
        "latency": _latency_summary(latencies),
        "pass": True,
    }
    print(json.dumps(payload, separators=(",", ":"), sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Ingress deep probes")
    sub = p.add_subparsers(dest="command", required=True)

    ws = sub.add_parser("ws_soak", help="Run websocket soak probe")
    ws.add_argument("--url", required=True)
    ws.add_argument("--host", required=True)
    ws.add_argument("--duration-seconds", type=int, default=600)
    ws.add_argument("--connections", type=int, default=50)
    ws.add_argument("--heartbeat-seconds", type=float, default=5.0)
    ws.add_argument("--timeout-seconds", type=float, default=5.0)
    ws.add_argument("--min-connected-ratio", type=float, default=0.90)
    ws.set_defaults(func=run_ws_soak)

    lb = sub.add_parser("lb_sample", help="Sample backend distribution via repeated HTTP requests")
    lb.add_argument("--url", required=True)
    lb.add_argument("--host", required=True)
    lb.add_argument("--strategy", default="round_robin")
    lb.add_argument("--requests", type=int, default=5000)
    lb.add_argument("--timeout-seconds", type=float, default=5.0)
    lb.add_argument("--min-backends", type=int, default=2)
    lb.add_argument("--max-skew-ratio", type=float, default=0.35)
    lb.add_argument("--require-distribution", action="store_true")
    lb.set_defaults(func=run_lb_sample)

    sticky = sub.add_parser("sticky_probe", help="Validate cookie stickiness over repeated requests")
    sticky.add_argument("--url", required=True)
    sticky.add_argument("--host", required=True)
    sticky.add_argument("--clients", type=int, default=3)
    sticky.add_argument("--requests-per-client", type=int, default=100)
    sticky.add_argument("--timeout-seconds", type=float, default=5.0)
    sticky.add_argument("--min-pin-ratio", type=float, default=0.95)
    sticky.set_defaults(func=run_sticky_probe)

    perf = sub.add_parser("http_bench", help="Collect HTTP throughput/latency metrics")
    perf.add_argument("--url", required=True)
    perf.add_argument("--host", required=True)
    perf.add_argument("--duration-seconds", type=float, default=180.0)
    perf.add_argument("--warmup-seconds", type=float, default=20.0)
    perf.add_argument("--concurrency", type=int, default=50)
    perf.add_argument("--timeout-seconds", type=float, default=5.0)
    perf.set_defaults(func=run_http_bench)

    return p


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
