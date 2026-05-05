#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from huggingface_hub import snapshot_download
except ImportError:  # pragma: no cover - exercised via runtime failure path
    snapshot_download = None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="bootstrap_inference_model.py",
        description="Download or reuse a test inference model at an exact local path.",
    )
    parser.add_argument("--model-id", required=True)
    parser.add_argument("--revision", default="")
    parser.add_argument("--local-path", required=True)
    parser.add_argument("--json", action="store_true", help="Emit JSON output.")
    return parser.parse_args(argv)


def _payload(
    *,
    status: str,
    model_id: str,
    revision: str | None,
    local_path: Path,
    result: str | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": status,
        "model_id": model_id,
        "local_path": str(local_path),
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }
    if revision:
        payload["revision"] = revision
    if result:
        payload["result"] = result
    if error:
        payload["error"] = error
    return payload


def ensure_model(*, model_id: str, revision: str | None, local_path: Path) -> dict[str, Any]:
    if not local_path.is_absolute():
        raise ValueError(f"local path must be absolute: {local_path}")
    if snapshot_download is None:
        raise RuntimeError("huggingface_hub is not installed")
    if (local_path / "config.json").is_file():
        return _payload(
            status="ready",
            result="reused",
            model_id=model_id,
            revision=revision,
            local_path=local_path,
        )
    local_path.mkdir(parents=True, exist_ok=True)
    snapshot_download(
        repo_id=model_id,
        revision=revision or None,
        local_dir=str(local_path),
    )
    return _payload(
        status="ready",
        result="downloaded",
        model_id=model_id,
        revision=revision,
        local_path=local_path,
    )


def _emit(payload: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, indent=2))
        return
    result = str(payload.get("result") or payload.get("status") or "").strip()
    model_id = str(payload.get("model_id") or "").strip()
    local_path = str(payload.get("local_path") or "").strip()
    print(f"{result}: {model_id} -> {local_path}")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    revision = str(args.revision or "").strip() or None
    local_path = Path(str(args.local_path)).expanduser()
    try:
        payload = ensure_model(
            model_id=str(args.model_id).strip(),
            revision=revision,
            local_path=local_path,
        )
    except Exception as exc:  # noqa: BLE001
        payload = _payload(
            status="failed",
            model_id=str(args.model_id).strip(),
            revision=revision,
            local_path=local_path,
            error=f"{exc.__class__.__name__}: {exc}",
        )
        _emit(payload, as_json=bool(args.json))
        return 1
    _emit(payload, as_json=bool(args.json))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
