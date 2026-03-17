import json

from ae.apishim import server as shim_server
from ae.apishim.store import ObjectStore
from tests.unit.test_apishim_storage import _handler, _json_body


def test_apishim_ha_mode_rejects_post_create(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AE_HA_MODE", "1")
    store = ObjectStore(tmp_path / "apishim.db")
    body = json.dumps(
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "demo", "namespace": "default"},
            "data": {"hello": "world"},
        }
    ).encode("utf-8")
    handler, status = _handler(
        store,
        monkeypatch,
        "/api/v1/namespaces/default/configmaps",
        method="POST",
        body=body,
    )

    handler.do_POST()

    assert status["code"] == 409
    payload = _json_body(handler)
    assert payload["reason"] == "HAUnsupported"
    assert "until H4" in payload["message"]
    assert store.get("", "v1", "configmaps", "default", "demo") is None


def test_apishim_ha_mode_rejects_put(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AE_HA_MODE", "1")
    store = ObjectStore(tmp_path / "apishim.db")
    body = json.dumps(
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "demo", "namespace": "default"},
            "data": {"hello": "world"},
        }
    ).encode("utf-8")
    handler, status = _handler(
        store,
        monkeypatch,
        "/api/v1/namespaces/default/configmaps/demo",
        method="PUT",
        body=body,
    )

    handler.do_PUT()

    assert status["code"] == 409
    payload = _json_body(handler)
    assert payload["reason"] == "HAUnsupported"
    assert store.get("", "v1", "configmaps", "default", "demo") is None


def test_apishim_ha_mode_rejects_patch(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AE_HA_MODE", "1")
    store = ObjectStore(tmp_path / "apishim.db")
    store.upsert(
        "",
        "v1",
        "configmaps",
        "default",
        "demo",
        metadata={"name": "demo", "namespace": "default"},
        spec={"hello": "world"},
        status={},
    )
    body = json.dumps({"data": {"hello": "again"}}).encode("utf-8")
    handler, status = _handler(
        store,
        monkeypatch,
        "/api/v1/namespaces/default/configmaps/demo",
        method="PATCH",
        body=body,
    )

    handler.do_PATCH()

    assert status["code"] == 409
    payload = _json_body(handler)
    assert payload["reason"] == "HAUnsupported"
    obj = store.get("", "v1", "configmaps", "default", "demo")
    assert obj is not None
    assert obj.spec == {"hello": "world"}


def test_apishim_ha_mode_rejects_delete(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AE_HA_MODE", "1")
    store = ObjectStore(tmp_path / "apishim.db")
    store.upsert(
        "",
        "v1",
        "configmaps",
        "default",
        "demo",
        metadata={"name": "demo", "namespace": "default"},
        spec={"hello": "world"},
        status={},
    )
    handler, status = _handler(
        store,
        monkeypatch,
        "/api/v1/namespaces/default/configmaps/demo",
        method="DELETE",
    )

    handler.do_DELETE()

    assert status["code"] == 409
    payload = _json_body(handler)
    assert payload["reason"] == "HAUnsupported"
    assert store.get("", "v1", "configmaps", "default", "demo") is not None


def test_apishim_ha_mode_keeps_get_available(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AE_HA_MODE", "1")
    store = ObjectStore(tmp_path / "apishim.db")
    store.upsert(
        "",
        "v1",
        "configmaps",
        "default",
        "demo",
        metadata={"name": "demo", "namespace": "default"},
        spec={"hello": "world"},
        status={},
    )
    handler, status = _handler(
        store,
        monkeypatch,
        "/api/v1/namespaces/default/configmaps",
        method="GET",
    )

    handler.do_GET()

    assert status["code"] == 200
    payload = _json_body(handler)
    items = payload.get("items") or []
    assert any((item.get("metadata") or {}).get("name") == "demo" for item in items)


def test_apishim_ha_mode_keeps_watch_available(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AE_HA_MODE", "1")
    store = ObjectStore(tmp_path / "apishim.db")
    watched: dict[str, object] = {}

    def fake_stream_watch(self, group, version, plural, namespace, query, transform=None) -> None:
        watched.update(
            {
                "group": group,
                "version": version,
                "plural": plural,
                "namespace": namespace,
                "watch": (query.get("watch") or ["0"])[0],
            }
        )
        self._ok({"kind": "Status", "status": "Success"})

    monkeypatch.setattr(shim_server.ShimHandler, "_stream_watch", fake_stream_watch)
    handler, status = _handler(
        store,
        monkeypatch,
        "/apis/apps/v1/namespaces/default/statefulsets?watch=1",
        method="GET",
    )

    handler.do_GET()

    assert status["code"] == 200
    assert watched == {
        "group": "apps",
        "version": "v1",
        "plural": "statefulsets",
        "namespace": "default",
        "watch": "1",
    }


def test_apishim_ha_mode_keeps_authorization_reviews_available(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AE_HA_MODE", "1")
    store = ObjectStore(tmp_path / "apishim.db")
    body = json.dumps(
        {
            "spec": {
                "resourceAttributes": {
                    "verb": "get",
                    "resource": "pods",
                    "namespace": "default",
                }
            }
        }
    ).encode("utf-8")
    handler, status = _handler(
        store,
        monkeypatch,
        "/apis/authorization.k8s.io/v1/selfsubjectaccessreviews",
        method="POST",
        body=body,
    )

    handler.do_POST()

    assert status["code"] == 201
    payload = _json_body(handler)
    assert payload["kind"] == "SelfSubjectAccessReview"
    assert "status" in payload
