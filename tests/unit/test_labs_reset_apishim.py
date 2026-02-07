from __future__ import annotations

import io
import json
from typing import Any

from ae.apishim.store import ObjectStore
from ae.observability.http_api import _ApiHandler


class _Headers:
    def __init__(self, mapping: dict[str, str]) -> None:
        self._m = {str(k): str(v) for k, v in mapping.items()}

    def get(self, key: str, default: str | None = None) -> str | None:  # noqa: D401
        return self._m.get(key, default)


class _Store:
    def list_status(self) -> list[object]:
        return []

    def list_registered_app_names(self) -> list[str]:
        return []


def _make_handler(path: str, payload: dict[str, Any]) -> tuple[_ApiHandler, io.BytesIO, list[int]]:
    body = json.dumps(payload).encode("utf-8")
    rfile = io.BytesIO(body)
    wfile = io.BytesIO()

    h = object.__new__(_ApiHandler)
    h.path = path  # type: ignore[attr-defined]
    h.headers = _Headers({"Content-Type": "application/json", "Content-Length": str(len(body))})  # type: ignore[attr-defined]
    h.rfile = rfile  # type: ignore[attr-defined]
    h.wfile = wfile  # type: ignore[attr-defined]

    status: list[int] = []

    def _send_response(code: int) -> None:
        status.clear()
        status.append(int(code))

    def _noop(*_args: Any, **_kwargs: Any) -> None:  # noqa: ANN401
        return None

    h.send_response = _send_response  # type: ignore[attr-defined]
    h.send_header = _noop  # type: ignore[attr-defined]
    h.end_headers = _noop  # type: ignore[attr-defined]
    h.store = _Store()  # type: ignore[attr-defined]
    h.delete_fn = lambda *_args, **_kwargs: {"removed": 0}  # type: ignore[attr-defined]

    return h, wfile, status


def test_labs_reset_uses_profile_apishim_db(tmp_path, monkeypatch) -> None:  # noqa: D401
    profile_dir = tmp_path / "profiles" / "dev-min"
    profile_dir.mkdir(parents=True, exist_ok=True)
    db_path = profile_dir / "apishim.db"

    store = ObjectStore(db_path=db_path)
    store.upsert(
        "apps",
        "v1",
        "daemonsets",
        "demo-helm",
        "demochart-ds",
        {"name": "demochart-ds", "namespace": "demo-helm"},
        {},
        {},
    )
    store.upsert(
        "apps",
        "v1",
        "statefulsets",
        "demo-helm",
        "demochart-sts",
        {"name": "demochart-sts", "namespace": "demo-helm"},
        {},
        {},
    )

    env_file = profile_dir / "apishim.env"
    env_file.write_text(f"AE_APISHIM_DB={db_path}\n", encoding="utf-8")

    monkeypatch.setenv("AE_LABS", "1")
    monkeypatch.delenv("AE_LABS_TOKEN", raising=False)
    monkeypatch.setenv("DEV_PROFILE_DIR", str(profile_dir))
    monkeypatch.delenv("AE_APISHIM_DB", raising=False)
    monkeypatch.delenv("AE_LABS_HELM_TOKEN", raising=False)
    monkeypatch.delenv("AE_LABS_HELM_SERVER", raising=False)

    h, _wfile, status = _make_handler("/labs/reset", {"session_id": "abc"})
    _ApiHandler._handle_labs_post(h)  # type: ignore[arg-type]
    assert status and status[0] == 200

    store_after = ObjectStore(db_path=db_path)
    assert store_after.list("apps", "v1", "daemonsets", "demo-helm") == []
    assert store_after.list("apps", "v1", "statefulsets", "demo-helm") == []
