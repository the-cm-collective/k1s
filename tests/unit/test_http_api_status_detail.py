from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ae.controller.reconciler import Reconciler
from ae.controller.spec import AppManifest, AppSpec, IngressSpec, Metadata
from ae.controller.state import SQLiteStateStore
from ae.observability.http_api import _ApiHandler
from ae.runtime.base import PodState, RuntimeAdapter, RuntimeResult


class _StubRuntime(RuntimeAdapter):
    def ensure_app(
        self,
        manifest: AppManifest,
        revision: int,
        *,
        keep_old: bool = False,
        limit_create: int | None = None,
        pod_names: list[str] | None = None,
        node_id: str | None = None,
    ) -> RuntimeResult:  # type: ignore[override]
        _ = (manifest, keep_old, limit_create, node_id)
        names = pod_names or [f"backend-rev{revision}-{idx}" for idx in range(2)]
        pods = [PodState(pod_name=name, ready=True, status="running") for name in names]
        return RuntimeResult(
            revision=revision,
            created=len(pods),
            updated=0,
            removed=0,
            pod_states=pods,
        )


@pytest.fixture(autouse=True)
def _clear_read_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AE_API_READ_SCOPE", raising=False)


def _manifest() -> AppManifest:
    return AppManifest(
        apiVersion="ae.dev/v1alpha1",
        kind="Deployment",
        metadata=Metadata(name="backend", namespace="k1s-prof-workload"),
        spec=AppSpec(
            image="ghcr.io/example/backend:latest",
            replicas=2,
            ingress=IngressSpec(host="backend.workerbee.localhost", path="/"),
        ),
    )


def _seed_store(tmp_path: Path) -> SQLiteStateStore:
    store = SQLiteStateStore(tmp_path / "state.db")
    report = Reconciler(runtime=_StubRuntime(), state_store=store).reconcile(_manifest())
    assert report.app_name == "k1s-prof-workload--backend"
    assert report.revision_status == "ready"
    return store


def _handler(
    store: SQLiteStateStore,
    *,
    path: str = "/status/k1s-prof-workload--backend?details=1",
) -> tuple[_ApiHandler, dict[str, Any], list[int]]:
    handler = object.__new__(_ApiHandler)
    handler.store = store  # type: ignore[attr-defined]
    handler.path = path  # type: ignore[attr-defined]
    handler.headers = {}  # type: ignore[attr-defined]
    handler.command = "GET"  # type: ignore[attr-defined]

    captured: dict[str, Any] = {}
    responses: list[int] = []

    def _json_ok(payload: object) -> None:
        captured["payload"] = payload

    def _deny(code: int, message: str = "unauthorized") -> None:
        captured["deny"] = {"code": code, "message": message}

    def _send_response(code: int, _message: str | None = None) -> None:
        responses.append(code)

    handler._json_ok = _json_ok  # type: ignore[attr-defined]
    handler._deny = _deny  # type: ignore[attr-defined]
    handler.send_response = _send_response  # type: ignore[attr-defined]
    handler.end_headers = lambda: None  # type: ignore[attr-defined]
    return handler, captured, responses


def test_status_list_includes_namespaced_app_key(tmp_path: Path) -> None:
    store = _seed_store(tmp_path)
    handler, captured, _responses = _handler(store, path="/status")

    _ApiHandler._handle_status_list(handler)  # type: ignore[arg-type]

    payload = captured["payload"]
    assert isinstance(payload, dict)
    items = payload["items"]
    assert [item["app_name"] for item in items] == ["k1s-prof-workload--backend"]
    assert items[0]["namespace"] == "k1s-prof-workload"
    assert items[0]["name"] == "backend"


def test_status_detail_resolves_exact_namespaced_app_key(tmp_path: Path) -> None:
    store = _seed_store(tmp_path)
    handler, captured, _responses = _handler(store)

    _ApiHandler._handle_status_single(
        handler,
        "k1s-prof-workload--backend?details=1",
    )  # type: ignore[arg-type]

    payload = captured["payload"]
    assert payload["app_name"] == "k1s-prof-workload--backend"
    assert payload["namespace"] == "k1s-prof-workload"
    assert payload["name"] == "backend"
    assert payload["desired_replicas"] == 2
    assert payload["ready_replicas"] == 2
    assert payload["live_replicas"] == 2
    assert payload["manifest"]["metadata"]["namespace"] == "k1s-prof-workload"
    assert len(payload["pods"]) == 2
    assert payload["replicas"] == payload["pods"]


def test_status_detail_resolves_encoded_display_ref(tmp_path: Path) -> None:
    store = _seed_store(tmp_path)
    handler, captured, _responses = _handler(
        store,
        path="/status/k1s-prof-workload%2Fbackend?details=1",
    )

    _ApiHandler._handle_status_single(
        handler,
        "k1s-prof-workload%2Fbackend?details=1",
    )  # type: ignore[arg-type]

    payload = captured["payload"]
    assert payload["app_name"] == "k1s-prof-workload--backend"
    assert payload["manifest"]["metadata"]["name"] == "backend"
    assert len(payload["pods"]) == 2


def test_status_detail_resolves_unique_short_name(tmp_path: Path) -> None:
    store = _seed_store(tmp_path)
    handler, captured, _responses = _handler(store, path="/status/backend?details=1")

    _ApiHandler._handle_status_single(handler, "backend?details=1")  # type: ignore[arg-type]

    assert captured["payload"]["app_name"] == "k1s-prof-workload--backend"


def test_status_detail_read_scope_uses_resolved_app_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _seed_store(tmp_path)
    monkeypatch.setenv("AE_API_READ_SCOPE", "k1s-prof-workload--*")
    handler, captured, _responses = _handler(
        store,
        path="/status/k1s-prof-workload%2Fbackend?details=1",
    )

    _ApiHandler._handle_status_single(
        handler,
        "k1s-prof-workload%2Fbackend?details=1",
    )  # type: ignore[arg-type]

    assert captured["payload"]["app_name"] == "k1s-prof-workload--backend"

    monkeypatch.setenv("AE_API_READ_SCOPE", "other--*")
    handler, captured, _responses = _handler(
        store,
        path="/status/k1s-prof-workload%2Fbackend?details=1",
    )

    _ApiHandler._handle_status_single(
        handler,
        "k1s-prof-workload%2Fbackend?details=1",
    )  # type: ignore[arg-type]

    assert captured["deny"] == {"code": 403, "message": "unauthorized"}
    assert "payload" not in captured


def test_dashboard_template_surfaces_detail_fetch_errors() -> None:
    html = Path("src/ae/resources/observability/dashboard.html").read_text(encoding="utf-8")

    assert 'id="detail-error"' in html
    assert "function showDetailError(app, err)" in html
    assert "Failed to load details for " in html
    assert "}).catch(function(err)" in html
    assert "showDetailError(requested, err);" in html
