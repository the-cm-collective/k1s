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


def _make_handler(
    path: str,
    payload: dict[str, Any],
    *,
    store: object | None = None,
    delete_fn: object | None = None,
) -> tuple[_ApiHandler, io.BytesIO, list[int]]:
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
    h.store = store or _Store()  # type: ignore[attr-defined]
    h.delete_fn = delete_fn or (lambda *_args, **_kwargs: {"removed": 0})  # type: ignore[attr-defined]

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


def test_labs_reset_waits_out_lingering_helm_adapter_readds(monkeypatch) -> None:
    class _Status:
        def __init__(self, app_name: str) -> None:
            self.app_name = app_name

    class _RacingStore:
        def __init__(self) -> None:
            self._status_calls = 0
            self._registered_calls = 0

        def list_status(self) -> list[object]:
            self._status_calls += 1
            if self._status_calls == 1:
                return [_Status("demo-helm--demochart-ds")]
            if self._status_calls == 3:
                return [_Status("demo-helm--demochart-ds")]
            return []

        def list_registered_app_names(self) -> list[str]:
            self._registered_calls += 1
            if self._registered_calls == 1:
                return ["demo-helm--demochart-ds", "demo-helm--demochart-sts"]
            return []

    deleted: list[str] = []

    def _delete_fn(app: str, _purge: bool) -> dict[str, Any]:
        deleted.append(app)
        return {"app": app, "removed": 1, "purged": True}

    monkeypatch.setenv("AE_LABS", "1")
    monkeypatch.delenv("AE_LABS_TOKEN", raising=False)
    monkeypatch.setattr("ae.observability.http_api.time.sleep", lambda _s: None)

    h, _wfile, status = _make_handler(
        "/labs/reset",
        {"session_id": "abc"},
        store=_RacingStore(),
        delete_fn=_delete_fn,
    )
    _ApiHandler._handle_labs_post(h)  # type: ignore[arg-type]

    assert status and status[0] == 200
    assert deleted == [
        "echo-abc",
        "demo-helm--demochart-ds",
        "demo-helm--demochart-sts",
        "demo-helm--demochart-ds",
    ]


def test_labs_reset_prefers_apishim_server_and_cleans_survivors(
    tmp_path, monkeypatch
) -> None:
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
    env_file.write_text(
        "\n".join(
            [
                f"AE_APISHIM_DB={db_path}",
                "AE_APISHIM_SERVER=https://127.0.0.1:8445",
                "AE_LABS_HELM_SERVER=https://127.0.0.1:8455",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    calls: list[tuple[str, str]] = []

    class _Resp:
        def __init__(self, status_code: int, payload: dict[str, Any] | None = None) -> None:
            self.status_code = int(status_code)
            self._payload = payload if payload is not None else {}
            self.content = (
                json.dumps(self._payload).encode("utf-8") if payload is not None else b""
            )

        def json(self) -> dict[str, Any]:
            return self._payload

    def _fake_get(url: str, **_kwargs: Any) -> _Resp:
        calls.append(("GET", url))
        if url == "https://127.0.0.1:8445/version":
            return _Resp(200, {"gitVersion": "v1.29.0-k1s-shim"})
        if ":8455" in url:
            return _Resp(500, {})
        if url.endswith("/daemonsets"):
            return _Resp(
                200,
                {"items": [{"metadata": {"name": "demochart-ds", "namespace": "demo-helm"}}]},
            )
        if url.endswith("/statefulsets"):
            return _Resp(
                200,
                {"items": [{"metadata": {"name": "demochart-sts", "namespace": "demo-helm"}}]},
            )
        return _Resp(200, {"items": []})

    def _fake_delete(url: str, **_kwargs: Any) -> _Resp:
        calls.append(("DELETE", url))
        return _Resp(500, {})

    monkeypatch.setenv("AE_LABS", "1")
    monkeypatch.delenv("AE_LABS_TOKEN", raising=False)
    monkeypatch.setenv("DEV_PROFILE_DIR", str(profile_dir))
    monkeypatch.setenv("AE_APISHIM_SERVER", "https://127.0.0.1:8445")
    monkeypatch.setenv("AE_LABS_HELM_SERVER", "https://127.0.0.1:8455")
    monkeypatch.setenv("AE_APISHIM_TOKEN", "demo-token")
    monkeypatch.delenv("AE_APISHIM_DB", raising=False)
    monkeypatch.delenv("AE_APISHIM_DSN", raising=False)
    monkeypatch.setattr("requests.get", _fake_get)
    monkeypatch.setattr("requests.delete", _fake_delete)

    h, _wfile, status = _make_handler("/labs/reset", {"session_id": "abc"})
    _ApiHandler._handle_labs_post(h)  # type: ignore[arg-type]
    assert status and status[0] == 200

    assert any(url == "https://127.0.0.1:8445/version" for _method, url in calls)
    assert all(":8455" not in url for _method, url in calls)

    store_after = ObjectStore(db_path=db_path)
    assert store_after.list("apps", "v1", "daemonsets", "demo-helm") == []
    assert store_after.list("apps", "v1", "statefulsets", "demo-helm") == []
