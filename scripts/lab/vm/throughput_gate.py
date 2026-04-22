#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path


def _load(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser(description="Compare throughput run with baseline")
    ap.add_argument("--baseline", required=True)
    ap.add_argument("--current", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--min-tps-ratio", type=float, default=0.90)
    ap.add_argument("--max-p95-ratio", type=float, default=1.20)
    ap.add_argument("--max-error-rate", type=float, default=0.001)
    args = ap.parse_args()

    baseline = _load(args.baseline)
    current = _load(args.current)

    b_tps = float(baseline.get("tokens_out_per_s") or 0.0)
    c_tps = float(current.get("tokens_out_per_s") or 0.0)
    b_p95 = float(baseline.get("latency_p95") or 0.0)
    c_p95 = float(current.get("latency_p95") or 0.0)
    c_err = float(current.get("error_rate") or 1.0)

    tps_ratio = (c_tps / b_tps) if b_tps > 0 else 0.0
    p95_ratio = (c_p95 / b_p95) if b_p95 > 0 else 99.0

    checks = {
        "tokens_out_per_s": tps_ratio >= args.min_tps_ratio,
        "latency_p95": p95_ratio <= args.max_p95_ratio,
        "error_rate": c_err <= args.max_error_rate,
    }
    passed = all(checks.values())

    result = {
        "passed": passed,
        "checks": checks,
        "ratios": {
            "tokens_out_per_s": round(tps_ratio, 6),
            "latency_p95": round(p95_ratio, 6),
        },
        "thresholds": {
            "min_tps_ratio": args.min_tps_ratio,
            "max_p95_ratio": args.max_p95_ratio,
            "max_error_rate": args.max_error_rate,
        },
        "baseline": args.baseline,
        "current": args.current,
    }

    Path(args.out).write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
