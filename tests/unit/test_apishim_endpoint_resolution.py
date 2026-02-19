from __future__ import annotations

from types import SimpleNamespace

from ae.apishim import server as shim_server


def _mk_handler(server_obj):
    handler = object.__new__(shim_server.ShimHandler)
    handler.server = server_obj
    return handler


def test_normalize_runtime_endpoint_rewrites_loopback(monkeypatch):
    monkeypatch.setenv("AE_NODE_ADVERTISE_IP", "host.containers.internal")
    out = shim_server.ShimHandler._normalize_runtime_endpoint("http://127.0.0.1:9112")
    assert out == "http://host.containers.internal:9112"


def test_normalize_runtime_endpoint_rewrites_unresolvable_host(monkeypatch):
    monkeypatch.setenv("AE_NODE_ADVERTISE_IP", "host.containers.internal")

    def _boom(*_args, **_kwargs):
        raise OSError("not resolvable")

    monkeypatch.setattr(shim_server.socket, "getaddrinfo", _boom)
    out = shim_server.ShimHandler._normalize_runtime_endpoint("http://h4ckt0p:9112")
    assert out == "http://host.containers.internal:9112"


def test_normalize_runtime_endpoint_keeps_resolvable_host(monkeypatch):
    monkeypatch.setenv("AE_NODE_ADVERTISE_IP", "host.containers.internal")

    def _ok(*_args, **_kwargs):
        return [(0, 0, 0, "", ("127.0.0.1", 0))]

    monkeypatch.setattr(shim_server.socket, "getaddrinfo", _ok)
    endpoint = "http://example.local:9112"
    out = shim_server.ShimHandler._normalize_runtime_endpoint(endpoint)
    assert out == endpoint


def test_runtime_for_endpoint_uses_rewritten_cache_key(monkeypatch):
    monkeypatch.setenv("AE_NODE_ADVERTISE_IP", "host.containers.internal")

    def _boom(*_args, **_kwargs):
        raise OSError("not resolvable")

    monkeypatch.setattr(shim_server.socket, "getaddrinfo", _boom)

    base_runtime = object()
    server_obj = SimpleNamespace(
        runtime=base_runtime,
        _runtime_base=base_runtime,
        _runtime_cache={},
        _agent_url=None,
    )
    handler = _mk_handler(server_obj)

    rt1 = handler._runtime_for_endpoint("http://h4ckt0p:9112")
    assert getattr(rt1, "_agent_url", None) == "http://host.containers.internal:9112"

    rt2 = handler._runtime_for_endpoint("http://h4ckt0p:9112")
    assert rt2 is rt1


def test_runtime_for_endpoint_matches_agent_url_after_normalization(monkeypatch):
    monkeypatch.setenv("AE_NODE_ADVERTISE_IP", "host.containers.internal")

    def _boom(*_args, **_kwargs):
        raise OSError("not resolvable")

    monkeypatch.setattr(shim_server.socket, "getaddrinfo", _boom)
    base_runtime = object()
    server_obj = SimpleNamespace(
        runtime=base_runtime,
        _runtime_base=base_runtime,
        _runtime_cache={},
        _agent_url="http://h4ckt0p:9112",
    )
    handler = _mk_handler(server_obj)
    rt = handler._runtime_for_endpoint("http://h4ckt0p:9112")
    assert rt is base_runtime
