
from ae.runtime import ports


def test_choose_host_port_prefers_requested_when_available(monkeypatch):
    monkeypatch.setattr(ports, "_port_is_free", lambda port: port == 12345)
    chosen, used_preferred = ports.choose_host_port(12345)
    assert chosen == 12345
    assert used_preferred is True


def test_choose_host_port_falls_back_when_busy(monkeypatch):
    checked = []

    def fake_port_free(port):
        checked.append(port)
        return port == 12347

    monkeypatch.setattr(ports, "_port_is_free", fake_port_free)
    chosen, used_preferred = ports.choose_host_port(12345, search_span=5)
    assert chosen == 12347
    assert used_preferred is False
    assert checked[0] == 12345  # ensure preferred was attempted first


def test_choose_host_port_respects_reserved(monkeypatch):
    monkeypatch.setattr(ports, "_port_is_free", lambda _port: True)
    reserved = {12345}
    chosen, used_preferred = ports.choose_host_port(12345, reserved=reserved, search_span=3)
    assert chosen == 12346
    assert used_preferred is False
    assert 12346 in reserved


def test_choose_host_port_respects_blocked(monkeypatch):
    monkeypatch.setattr(ports, "_port_is_free", lambda _port: True)
    blocked = {12345, 12346}
    chosen, used_preferred = ports.choose_host_port(12345, blocked=blocked, search_span=5)
    assert chosen == 12344
    assert used_preferred is False
