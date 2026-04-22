from __future__ import annotations

import subprocess

from ae.node import net_helper


def test_bridge_addr_for_cidr_uses_first_host_address() -> None:
    assert net_helper._bridge_addr_for_cidr("10.42.0.0/24") == "10.42.0.1/24"


def test_bridge_addr_for_cidr_preserves_invalid_input() -> None:
    assert net_helper._bridge_addr_for_cidr("not-a-cidr") == "not-a-cidr"


def test_ensure_pod_bridge_assigns_gateway_address(monkeypatch) -> None:
    calls: list[list[str]] = []
    iptables_calls: list[tuple[str, list[str], str | None, int | None]] = []

    monkeypatch.setattr(net_helper, "_run", lambda cmd: calls.append(cmd))
    monkeypatch.setattr(
        net_helper,
        "_ensure_iptables_rule",
        lambda chain, rule, table=None, position=None: iptables_calls.append(
            (chain, rule, table, position)
        ),
    )

    net_helper.ensure_pod_bridge("cni0", "10.42.0.0/24")

    assert [net_helper.IP_BIN, "addr", "add", "10.42.0.1/24", "dev", "cni0"] in calls
    assert ("FORWARD", ["-i", "cni0", "-j", "ACCEPT"], None, 1) in iptables_calls
    assert ("FORWARD", ["-o", "cni0", "-j", "ACCEPT"], None, 1) in iptables_calls
    assert (
        "POSTROUTING",
        ["-s", "10.42.0.0/24", "!", "-d", "10.42.0.0/24", "-j", "MASQUERADE"],
        "nat",
        None,
    ) in iptables_calls


def test_ensure_iptables_rule_inserts_missing_rule(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 1)

    monkeypatch.setattr(net_helper.subprocess, "run", fake_run)
    monkeypatch.setattr(net_helper, "_run", lambda cmd: calls.append(cmd))

    net_helper._ensure_iptables_rule("FORWARD", ["-i", "cni0", "-j", "ACCEPT"], position=1)

    assert [net_helper.IPTABLES_BIN, "-C", "FORWARD", "-i", "cni0", "-j", "ACCEPT"] in calls
    assert [net_helper.IPTABLES_BIN, "-I", "FORWARD", "1", "-i", "cni0", "-j", "ACCEPT"] in calls


def test_ensure_iptables_rule_skips_existing_rule(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(net_helper.subprocess, "run", fake_run)
    monkeypatch.setattr(net_helper, "_run", lambda cmd: calls.append(cmd))

    net_helper._ensure_iptables_rule(
        "POSTROUTING",
        ["-s", "10.42.0.0/24", "!", "-d", "10.42.0.0/24", "-j", "MASQUERADE"],
        table="nat",
    )

    assert calls == [
        [
            net_helper.IPTABLES_BIN,
            "-t",
            "nat",
            "-C",
            "POSTROUTING",
            "-s",
            "10.42.0.0/24",
            "!",
            "-d",
            "10.42.0.0/24",
            "-j",
            "MASQUERADE",
        ]
    ]
