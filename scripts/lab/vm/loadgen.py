#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import secrets
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import requests

PROMPTS = [
    "Summarize what pipeline parallelism means in one paragraph.",
    "Explain service-level indicators for inference systems.",
    "Give a concise list of checks for a CRI GPU node.",
    "Describe why stable run IDs improve reproducibility.",
]


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    i = int(math.floor((len(values) - 1) * p))
    return values[max(0, min(i, len(values) - 1))]


def infer_tokens(payload: Any) -> tuple[int | None, int | None]:
    if not isinstance(payload, dict):
        return None, None
    usage = payload.get("usage")
    if isinstance(usage, dict):
        tin = usage.get("prompt_tokens") or usage.get("input_tokens")
        tout = usage.get("completion_tokens") or usage.get("output_tokens")
        try:
            return int(tin) if tin is not None else None, int(tout) if tout is not None else None
        except Exception:
            return None, None
    return None, None


def request_once(session: requests.Session, url: str, run_id: str, timeout: int) -> dict[str, Any]:
    prompt = secrets.choice(PROMPTS)
    t0 = time.time()
    rec: dict[str, Any] = {
        "run_id": run_id,
        "ts_start": now_iso(),
        "prompt": prompt,
        "status": None,
        "served_by": None,
        "latency_s": None,
        "tokens_in": None,
        "tokens_out": None,
    }
    payload = {
        "prompt": prompt,
        "max_tokens": 64,
        "temperature": 0.0,
    }
    try:
        r = session.post(
            url,
            json=payload,
            timeout=timeout,
            headers={"x-run-id": run_id},
        )
        rec["status"] = r.status_code
        rec["latency_s"] = round(time.time() - t0, 6)
        rec["served_by"] = r.headers.get("x-node-id") or r.headers.get("x-served-by")
        body: Any = None
        try:
            body = r.json()
        except Exception:
            body = None
        tin, tout = infer_tokens(body)
        rec["tokens_in"] = tin
        rec["tokens_out"] = tout
        if r.status_code >= 400:
            rec["error"] = (str(body) if body is not None else r.text)[:512]
    except Exception as exc:
        rec["status"] = "error"
        rec["latency_s"] = round(time.time() - t0, 6)
        rec["error"] = str(exc)
    rec["ts_end"] = now_iso()
    return rec


def summarize(records: list[dict[str, Any]], duration_s: float) -> dict[str, Any]:
    ok = [r for r in records if r.get("status") == 200]
    err = [r for r in records if r.get("status") != 200]
    lat = sorted(float(r["latency_s"]) for r in ok if isinstance(r.get("latency_s"), int | float))
    served_by_counts: dict[str, int] = {}
    tokens_out = 0
    for r in records:
        sb = str(r.get("served_by") or "unknown")
        served_by_counts[sb] = served_by_counts.get(sb, 0) + 1
        tout = r.get("tokens_out")
        if isinstance(tout, int):
            tokens_out += tout

    total = len(records)
    err_rate = (len(err) / total) if total else 1.0
    return {
        "total": total,
        "ok": len(ok),
        "err": len(err),
        "error_rate": round(err_rate, 6),
        "duration_s": round(duration_s, 3),
        "requests_per_s": round((len(ok) / duration_s) if duration_s > 0 else 0.0, 6),
        "tokens_out_total": tokens_out,
        "tokens_out_per_s": round((tokens_out / duration_s) if duration_s > 0 else 0.0, 6),
        "latency_p50": percentile(lat, 0.50),
        "latency_p95": percentile(lat, 0.95),
        "latency_p99": percentile(lat, 0.99),
        "latency_mean": round(statistics.fmean(lat), 6) if lat else None,
        "served_by_counts": served_by_counts,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Simple inference load generator")
    ap.add_argument("--url", required=True)
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--minutes", type=int, default=20)
    ap.add_argument("--concurrency", type=int, default=16)
    ap.add_argument("--timeout", type=int, default=60)
    ap.add_argument("--out", required=True)
    ap.add_argument("--summary", required=True)
    args = ap.parse_args()

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path = Path(args.summary)
    summary_path.parent.mkdir(parents=True, exist_ok=True)

    deadline = time.time() + (args.minutes * 60)
    lock = threading.Lock()
    records: list[dict[str, Any]] = []

    started = time.time()
    with (
        requests.Session() as session,
        ThreadPoolExecutor(max_workers=args.concurrency) as pool,
        out_path.open("w", encoding="utf-8") as fh,
    ):
        while time.time() < deadline:
            futures = [
                pool.submit(request_once, session, args.url, args.run_id, args.timeout)
                for _ in range(args.concurrency)
            ]
            for fut in as_completed(futures):
                rec = fut.result()
                with lock:
                    records.append(rec)
                fh.write(json.dumps(rec, ensure_ascii=True) + "\n")
                fh.flush()

    duration = max(0.001, time.time() - started)
    summary = summarize(records, duration)
    summary.update(
        {
            "run_id": args.run_id,
            "url": args.url,
            "minutes": args.minutes,
            "concurrency": args.concurrency,
            "finished_at": now_iso(),
        }
    )
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
