from __future__ import annotations

import json
import subprocess
from pathlib import Path

from scripts.dev import microk8s_stack_bundle


def test_bundle_renders_json_from_manual_args(capsys) -> None:
    code = microk8s_stack_bundle.main(
        [
            "--release",
            "k1s-dev-a",
            "--namespace",
            "k1s-dev-a",
            "--site-id",
            "host-a",
            "--stack-domain",
            "k1s-dev-a.home.arpa",
            "--wildcard-apps-domain",
            "apps.k1s-dev-a.home.arpa",
            "--registry-host",
            "registry.k1s.home.arpa:32000",
            "--controller-host",
            "192.168.29.15",
            "--nats-leaf-host",
            "192.168.29.16",
            "--rathole-host",
            "192.168.29.17",
            "--agent-token",
            "agent-secret",
            "--rathole-token",
            "rathole-secret",
            "--nats-leaf-user",
            "site-uplink",
            "--nats-leaf-password",
            "leaf-pass",
        ]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["controller_url"] == "http://192.168.29.15:9110"
    assert payload["nats_leaf_addr"] == "192.168.29.16:7422"
    assert payload["nats_leaf_url"] == "nats://site-uplink:leaf-pass@192.168.29.16:7422"
    assert payload["rathole_server_addr"] == "192.168.29.17:2333"
    env = payload["suggested_edge_env"]
    assert env["AE_CONTROLLER_URL"] == "http://192.168.29.15:9110"
    assert env["AE_NATS_HUB_LEAF_HOST"] == "192.168.29.16"
    assert env["AE_NATS_HUB_LEAF_PORT"] == "7422"


def test_bundle_renders_env_output(capsys) -> None:
    code = microk8s_stack_bundle.main(
        [
            "--release",
            "k1s-dev-a",
            "--namespace",
            "k1s-dev-a",
            "--site-id",
            "host-a",
            "--stack-domain",
            "k1s-dev-a.home.arpa",
            "--wildcard-apps-domain",
            "apps.k1s-dev-a.home.arpa",
            "--registry-host",
            "registry.k1s.home.arpa:32000",
            "--controller-host",
            "192.168.29.15",
            "--nats-leaf-host",
            "192.168.29.16",
            "--rathole-host",
            "192.168.29.17",
            "--agent-token",
            "agent-secret",
            "--rathole-token",
            "rathole-secret",
            "--format",
            "env",
        ]
    )
    assert code == 0
    text = capsys.readouterr().out
    assert "AE_CONTROLLER_URL=http://192.168.29.15:9110" in text
    assert "AE_NATS_HUB_LEAF_HOST=192.168.29.16" in text
    assert "AE_RATHOLE_SERVER_ADDR=192.168.29.17:2333" in text
    assert "K1S_STACK_DOMAIN=k1s-dev-a.home.arpa" in text


def test_bundle_reads_live_cluster_contract_from_kubectl(monkeypatch, capsys) -> None:
    configmap = {
        "data": {
            "stack_domain": "k1s-dev-a.home.arpa",
            "wildcard_apps_domain": "apps.k1s-dev-a.home.arpa",
            "registry_host": "registry.k1s.home.arpa:32000",
            "controller_external_service": "k1s-dev-a-k1s-core-ha-controller-external",
            "controller_external_port": "9110",
            "nats_leaf_external_service": "k1s-dev-a-k1s-core-ha-nats-leaf",
            "nats_leaf_port": "7422",
            "rathole_external_service": "k1s-dev-a-k1s-core-ha-rathole",
            "rathole_port": "2333",
            "auth_secret_name": "k1s-dev-a-k1s-core-ha-auth",
            "dash_url": "https://dash.k1s-dev-a.home.arpa/",
            "docs_url": "https://docs.k1s-dev-a.home.arpa/",
        }
    }
    secret = {
        "data": {
            "agent-token": "YWdlbnQtc2VjcmV0",
            "rathole-token": "cmF0aG9sZS1zZWNyZXQ=",
            "nats-leaf-user": "c2l0ZS11cGxpbms=",
            "nats-leaf-password": "bGVhZi1wYXNz",
        }
    }
    controller_service = {
        "status": {"loadBalancer": {"ingress": [{"ip": "192.168.29.15"}]}},
        "spec": {"clusterIP": "10.0.0.10"},
    }
    nats_service = {
        "status": {"loadBalancer": {"ingress": [{"hostname": "leaf.example.test"}]}},
        "spec": {"clusterIP": "10.0.0.11"},
    }
    rathole_service = {"status": {}, "spec": {"clusterIP": "10.0.0.12"}}

    payloads = {
        ("configmap", "k1s-dev-a-k1s-core-ha-bootstrap"): configmap,
        ("secret", "k1s-dev-a-k1s-core-ha-auth"): secret,
        ("service", "k1s-dev-a-k1s-core-ha-controller-external"): controller_service,
        ("service", "k1s-dev-a-k1s-core-ha-nats-leaf"): nats_service,
        ("service", "k1s-dev-a-k1s-core-ha-rathole"): rathole_service,
    }

    def fake_run(cmd, check, capture_output, text):  # noqa: ANN001
        kind = cmd[4]
        name = cmd[5]
        body = payloads[(kind, name)]
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(body), stderr="")

    monkeypatch.setattr(microk8s_stack_bundle.subprocess, "run", fake_run)
    code = microk8s_stack_bundle.main(
        [
            "--release",
            "k1s-dev-a",
            "--namespace",
            "k1s-dev-a",
            "--from-kube",
        ]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["controller_url"] == "http://192.168.29.15:9110"
    assert payload["nats_leaf_addr"] == "leaf.example.test:7422"
    assert payload["rathole_server_addr"] == "10.0.0.12:2333"
    assert payload["dash_url"] == "https://dash.k1s-dev-a.home.arpa/"
