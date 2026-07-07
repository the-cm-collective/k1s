"""CLI integration smoke tests."""

import argparse
import json
import stat
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from ae.cli.__main__ import handle_history, handle_rollout, handle_scale, main
from ae.controller.spec import AppManifest, AppSpec, Metadata
from ae.controller.state import RegistryConflictError, RegistryEntry, SQLiteStateStore


def write_manifest(path: Path) -> None:
    path.write_text(
        """
apiVersion: ae.dev/v1alpha1
kind: Deployment
metadata:
  name: echo
spec:
  image: alpine:3.20
  replicas: 1
        """.strip()
    )


def test_history_replica_alias_warns(tmp_path, capsys):
    store = SQLiteStateStore(tmp_path / "state.db")
    args = argparse.Namespace(
        name="echo",
        namespace=None,
        pod=None,
        replica="echo-rev1-0",
        limit=20,
        since=None,
        since_time=None,
        json=False,
    )
    global_args = argparse.Namespace(server=None, token=None)

    assert handle_history(args, store, global_args) == 0

    captured = capsys.readouterr()
    assert "history --replica is deprecated; use --pod" in captured.err


def test_remote_k8s_apply_posts_namespaced_rbac_before_workload(
    tmp_path, monkeypatch, capsys
) -> None:
    manifest_path = tmp_path / "mcp-dev-service.yaml"
    manifest_path.write_text(
        """
apiVersion: v1
kind: ServiceAccount
metadata:
  name: workerbee-k1s-mcp-hzn07-canary
  namespace: openstack-lite-mcp-dev
---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: hzn07-canary-deployment-executor
  namespace: openstack-lite-dashboard-proof
rules:
  - apiGroups: ["apps"]
    resources: ["deployments"]
    verbs: ["get", "list", "create", "delete"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: hzn07-canary-deployment-executor
  namespace: openstack-lite-dashboard-proof
subjects:
  - kind: ServiceAccount
    name: workerbee-k1s-mcp-hzn07-canary
    namespace: openstack-lite-mcp-dev
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: Role
  name: hzn07-canary-deployment-executor
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: workerbee-k1s-mcp-dev
  namespace: openstack-lite-mcp-dev
spec:
  template:
    spec:
      serviceAccountName: workerbee-k1s-mcp-hzn07-canary
      containers:
        - name: mcp
          image: openstack-lite-mcp-dev:hzn07
""".strip(),
        encoding="utf-8",
    )
    calls: list[tuple[str, str, str | None]] = []

    def fake_post(base: str, path: str, body: dict, token: str | None = None) -> dict:
        calls.append((base, path, token))
        if path == "/apply":
            assert body["apiVersion"] == "ae.dev/v1alpha1"
            assert body["metadata"]["name"] == "workerbee-k1s-mcp-dev"
            return {
                "status": "accepted",
                "app": "openstack-lite-mcp-dev/workerbee-k1s-mcp-dev",
                "resourceVersion": "7",
            }
        return {"kind": body["kind"]}

    monkeypatch.setenv("AE_APISHIM_SERVER", "https://127.0.0.1:18445")
    monkeypatch.setenv("AE_APISHIM_TOKEN", "apishim-token")
    monkeypatch.setattr("ae.cli.__main__._http_post_json", fake_post)

    exit_code = main(
        [
            "--server",
            "http://127.0.0.1:19108",
            "--token=secret-token",
            "apply",
            "--k8s",
            "-f",
            str(manifest_path),
        ]
    )

    assert exit_code == 0
    assert calls == [
        (
            "https://127.0.0.1:18445",
            "/api/v1/namespaces/openstack-lite-mcp-dev/serviceaccounts",
            "apishim-token",
        ),
        (
            "https://127.0.0.1:18445",
            "/apis/rbac.authorization.k8s.io/v1/namespaces/openstack-lite-dashboard-proof/roles",
            "apishim-token",
        ),
        (
            "https://127.0.0.1:18445",
            "/apis/rbac.authorization.k8s.io/v1/namespaces/openstack-lite-dashboard-proof/rolebindings",
            "apishim-token",
        ),
        ("http://127.0.0.1:19108", "/apply", "secret-token"),
    ]
    output = capsys.readouterr().out
    assert (
        "applied Kubernetes ServiceAccount "
        "openstack-lite-mcp-dev/workerbee-k1s-mcp-hzn07-canary"
    ) in output
    assert (
        "applied Kubernetes Role "
        "openstack-lite-dashboard-proof/hzn07-canary-deployment-executor"
    ) in output
    assert (
        "applied Kubernetes RoleBinding "
        "openstack-lite-dashboard-proof/hzn07-canary-deployment-executor"
    ) in output
    assert "applied desired state" in output


def test_remote_k8s_apply_rejects_cluster_rbac(tmp_path, monkeypatch, capsys) -> None:
    manifest_path = tmp_path / "cluster-role.yaml"
    manifest_path.write_text(
        """
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: bad-global-role
rules:
  - apiGroups: ["*"]
    resources: ["*"]
    verbs: ["*"]
""".strip(),
        encoding="utf-8",
    )

    def fake_post(*_args, **_kwargs):
        raise AssertionError("cluster-scoped RBAC must not be posted")

    monkeypatch.setattr("ae.cli.__main__._http_post_json", fake_post)

    exit_code = main(
        [
            "--server",
            "http://127.0.0.1:19108",
            "apply",
            "--k8s",
            "-f",
            str(manifest_path),
        ]
    )

    assert exit_code == 1
    assert "unsupported cluster-scoped Kubernetes kind: ClusterRole" in capsys.readouterr().out


def test_apply_and_status_commands(tmp_path, monkeypatch, capsys):
    manifest_path = tmp_path / "echo.yaml"
    write_manifest(manifest_path)

    db_path = tmp_path / "state.db"
    monkeypatch.setenv("AE_STATE_DB", str(db_path))
    monkeypatch.setenv("AE_RUNTIME_BACKEND", "stub")
    monkeypatch.setenv("AE_CADDY_SITES", "")
    registry_config = tmp_path / "registries.yaml"
    registry_config.write_text(
        """
ghcr.io:
  username: demo
  password: token
        """.strip()
    )
    monkeypatch.setenv("AE_REGISTRY_CONFIG", str(registry_config))

    exit_code = main(["apply", "-f", str(manifest_path)])
    assert exit_code == 0
    apply_out = capsys.readouterr().out
    assert "Applied default/echo" in apply_out

    exit_code = main(["status", "echo"])
    assert exit_code == 0
    status_out = capsys.readouterr().out
    assert "desired=1" in status_out
    assert "ready=1" in status_out
    assert "live=1" in status_out
    assert "rev=1" in status_out
    assert "ops=+1" in status_out
    assert "  - echo-rev1-0" in status_out

    exit_code = main(["status"])
    assert exit_code == 0
    list_out = capsys.readouterr().out
    assert "default/echo" in list_out
    assert "live=1" in list_out
    assert "rev=1" in list_out
    assert "ops=+1" in list_out

    exit_code = main(["status", "echo", "--history", "3"])
    assert exit_code == 0
    history_out = capsys.readouterr().out
    assert "history" in history_out
    assert "readiness" in history_out

    exit_code = main(["status", "echo", "--events"])
    assert exit_code == 0
    events_status_out = capsys.readouterr().out
    assert "event" in events_status_out

    # status json
    exit_code = main(["status", "echo", "--json", "--wide"])
    assert exit_code == 0
    status_json = capsys.readouterr().out
    assert '"app_name": "echo"' in status_json
    assert '"namespace": "default"' in status_json
    assert '"current_revision_ready_replicas": 1' in status_json
    assert '"current_revision_live_replicas": 1' in status_json
    assert '"old_revision_ready_replicas": 0' in status_json
    assert '"old_revision_live_replicas": 0' in status_json
    assert '"overlap_ready_replicas": 0' in status_json
    assert '"overlap_live_replicas": 0' in status_json

    exit_code = main(["metrics"])
    assert exit_code == 0
    metrics_out = capsys.readouterr().out
    assert "apps total" in metrics_out

    exit_code = main(["metrics", "--json"])
    assert exit_code == 0
    metrics_json = capsys.readouterr().out
    assert "total_apps" in metrics_json

    exit_code = main(["events", "echo", "--limit", "5"])
    assert exit_code == 0
    events_out = capsys.readouterr().out
    assert "ApplyCompleted" in events_out

    exit_code = main(["registry", "list"])
    assert exit_code == 0
    registry_out = capsys.readouterr().out
    assert "ghcr.io" in registry_out

    exit_code = main(["revisions", "echo"])
    assert exit_code == 0
    revisions_out = capsys.readouterr().out
    assert "rev 1" in revisions_out

    exit_code = main(["rollback", "echo"])
    assert exit_code == 1  # no previous revision yet


def test_logs_command(tmp_path, monkeypatch, capsys):
    db_path = tmp_path / "state.db"
    monkeypatch.setenv("AE_STATE_DB", str(db_path))
    monkeypatch.setenv("AE_RUNTIME_BACKEND", "stub")
    monkeypatch.setenv("AE_CADDY_SITES", "")

    # Non-existent app
    exit_code = main(["logs", "ghost"])
    assert exit_code == 1
    output = capsys.readouterr().out
    assert "No status recorded for default/ghost" in output

    # After apply, logs should print stub line
    manifest_path = tmp_path / "echo.yaml"
    write_manifest(manifest_path)
    assert main(["apply", "-f", str(manifest_path)]) == 0
    capsys.readouterr()
    exit_code = main(["logs", "echo"])
    assert exit_code == 0
    output = capsys.readouterr().out
    assert "echo-rev1-0" in output


def test_nodes_json_includes_typed_accelerators(tmp_path, monkeypatch, capsys):
    db_path = tmp_path / "state.db"
    monkeypatch.setenv("AE_STATE_DB", str(db_path))
    monkeypatch.setenv("AE_RUNTIME_BACKEND", "stub")
    monkeypatch.setenv("AE_CADDY_SITES", "")

    store = SQLiteStateStore(db_path)
    store.upsert_node(
        "edge-b--gpu-1",
        name="edge-b--gpu-1",
        labels={"site": "edge-b"},
        capabilities={
            "accelerators": [
                {
                    "id": "gpu-0",
                    "kind": "discrete_gpu",
                    "vendor": "nvidia",
                    "family": "RTX 8000",
                    "device_count": 1,
                    "memory_model": "dedicated",
                    "memory_bytes_per_device": 49152 * 1024 * 1024,
                    "runtime_handlers": ["nvidia"],
                    "partitioning_mode": "none",
                    "backing_device_id": None,
                    "execution_role": "execution",
                }
            ]
        },
        endpoint="http://edge-b.lan:9112",
    )
    store.record_heartbeat("edge-b--gpu-1", "Ready")

    exit_code = main(["nodes", "--json"])
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["count"] == 1
    assert payload["nodes"][0]["node_id"] == "edge-b--gpu-1"
    assert payload["nodes"][0]["gpu_count"] == 1
    assert payload["nodes"][0]["gpu_models"] == "RTX 8000"
    assert payload["nodes"][0]["capabilities"]["accelerators"][0]["vendor"] == "nvidia"


def test_rollback_command(tmp_path, monkeypatch, capsys):
    manifest_v1 = tmp_path / "echo.yaml"
    write_manifest(manifest_v1)

    manifest_v2 = tmp_path / "echo-v2.yaml"
    manifest_v2.write_text(
        """
apiVersion: ae.dev/v1alpha1
kind: Deployment
metadata:
  name: echo
spec:
  image: alpine:3.21
  replicas: 1
        """.strip()
    )

    db_path = tmp_path / "state.db"
    monkeypatch.setenv("AE_STATE_DB", str(db_path))
    monkeypatch.setenv("AE_RUNTIME_BACKEND", "stub")
    monkeypatch.setenv("AE_CADDY_SITES", "")

    assert main(["apply", "-f", str(manifest_v1)]) == 0
    capsys.readouterr()
    assert main(["apply", "-f", str(manifest_v2)]) == 0
    capsys.readouterr()

    exit_code = main(["rollback", "echo", "--to", "1"])
    assert exit_code == 0
    output = capsys.readouterr().out
    assert "Rolled back default/echo" in output


def test_api_tokens_with_ttl_and_state(tmp_path):
    dest = tmp_path / ".env.api"
    state = tmp_path / "tokens.json"
    exit_code = main(
        ["api", "tokens", "--generate", "--ttl-hours", "1", "-o", str(dest), "--state", str(state)]
    )
    assert exit_code == 0
    out = dest.read_text()
    assert "AE_API_ADMIN_TOKEN=" in out and "AE_API_ADMIN_TOKEN_EXPIRES=" in out
    payload = state.read_text()
    assert "generated_at" in payload and "admin" in payload


def test_examples_write_multiport(tmp_path):
    from ae.cli.__main__ import main as _main

    out_path = tmp_path / "echo-mp.yaml"
    exit_code = _main(["examples", "write", "--type", "multiport", "-o", str(out_path)])
    assert exit_code == 0
    text = out_path.read_text()
    assert "echo-multi" in text
    assert "kind: Deployment" in text


def test_apply_in_ha_mode_writes_registry_without_reconcile(tmp_path, monkeypatch, capsys):
    manifest_path = tmp_path / "echo.yaml"
    write_manifest(manifest_path)

    db_path = tmp_path / "state.db"
    monkeypatch.setenv("AE_STATE_DB", str(db_path))
    monkeypatch.setenv("AE_RUNTIME_BACKEND", "stub")
    monkeypatch.setenv("AE_CADDY_SITES", "")
    monkeypatch.setenv("AE_HA_MODE", "1")

    exit_code = main(["apply", "-f", str(manifest_path)])
    assert exit_code == 0
    output = capsys.readouterr().out
    assert "Applied desired state for default/echo" in output

    from ae.controller.state import SQLiteStateStore

    store = SQLiteStateStore(db_path)
    entry = store.get_registered_entry("echo")
    assert entry is not None
    assert entry.resource_version == 1
    assert store.list_status() == []


def test_rollout_restart_creates_new_revision_for_unchanged_manifest(
    tmp_path, monkeypatch, capsys
):
    manifest_path = tmp_path / "echo.yaml"
    write_manifest(manifest_path)

    db_path = tmp_path / "state.db"
    monkeypatch.setenv("AE_STATE_DB", str(db_path))
    monkeypatch.setenv("AE_RUNTIME_BACKEND", "stub")
    monkeypatch.setenv("AE_CADDY_SITES", "")

    assert main(["apply", "-f", str(manifest_path)]) == 0
    capsys.readouterr()

    assert main(["rollout", "restart", "echo"]) == 0
    output = capsys.readouterr().out

    assert "rollout restart default/echo: rev=2 status=ready" in output
    assert "restartAt=" in output
    store = SQLiteStateStore(db_path)
    revisions = store.list_revisions("echo", limit=2)
    assert [item.revision for item in revisions] == [2, 1]
    latest = store.get_revision_manifest("echo", 2)
    assert latest.spec.rollout is not None
    assert latest.spec.rollout["restartAt"]


def test_handle_scale_reports_registry_conflict(capsys):
    manifest = AppManifest(
        apiVersion="ae.dev/v1alpha1",
        kind="Deployment",
        metadata=Metadata(name="echo"),
        spec=AppSpec(image="alpine:3.20", replicas=1),
    )

    class _Store:
        def list_revisions(self, _name, limit=1):
            return [SimpleNamespace(revision=1)]

        def get_revision_manifest(self, _name, _revision):
            return manifest

        def get_registered_entry(self, _name):
            return RegistryEntry(
                app_name="echo",
                manifest=manifest,
                spec_hash="hash",
                source="cli",
                labels={},
                updated_at=datetime.now(timezone.utc),
                resource_version=1,
            )

        def register_app(self, *_args, **_kwargs):
            raise RegistryConflictError("echo", expected=1, actual=2)

    args = argparse.Namespace(name="echo", replicas=2, namespace=None)
    result = handle_scale(args, _Store(), SimpleNamespace())
    assert result == 1
    assert "scale conflict" in capsys.readouterr().out


def test_handle_rollout_restart_reports_registry_conflict(capsys):
    manifest = AppManifest(
        apiVersion="ae.dev/v1alpha1",
        kind="Deployment",
        metadata=Metadata(name="echo"),
        spec=AppSpec(image="alpine:3.20", replicas=1),
    )

    class _Store:
        def list_revisions(self, _name, limit=1):
            return [SimpleNamespace(revision=1)]

        def get_revision_manifest(self, _name, _revision):
            return manifest

        def get_registered_entry(self, _name):
            return RegistryEntry(
                app_name="echo",
                manifest=manifest,
                spec_hash="hash",
                source="cli",
                labels={},
                updated_at=datetime.now(timezone.utc),
                resource_version=1,
            )

        def register_app(self, *_args, **_kwargs):
            raise RegistryConflictError("echo", expected=1, actual=2)

    args = argparse.Namespace(name="echo", rollout_cmd="restart", namespace=None)
    result = handle_rollout(args, _Store(), SimpleNamespace())
    assert result == 1
    assert "rollout conflict" in capsys.readouterr().out


def _write_fake_kubectl(path: Path, *, fail_probe: bool = False) -> Path:
    path.write_text(
        f"""#!/usr/bin/env python3
import json
import os
import sys

argv = sys.argv[1:]
log_path = os.environ.get("AE_TEST_KUBECTL_LOG")
if log_path:
    with open(log_path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({{"argv": argv, "kubeconfig": os.environ.get("KUBECONFIG")}}) + "\\n")

if "get" in argv and "--raw" in argv and "/openapi/v2" in argv:
    if {str(fail_probe)}:
        print("dial tcp 127.0.0.1:8080: connect: connection refused", file=sys.stderr)
        sys.exit(1)
    print('{{}}')
    sys.exit(0)

if "apply" in argv:
    print("server dry run ok")
    sys.exit(0)

if "create" in argv and "namespace" in argv:
    print("namespace/demo created")
    sys.exit(0)

if "rollout" in argv and "status" in argv:
    print("deployment successfully rolled out")
    sys.exit(0)

if "delete" in argv:
    sys.exit(0)

sys.exit(0)
"""
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def test_k8s_report_fails_fast_when_kube_api_is_unreachable(tmp_path, capsys):
    manifest_path = tmp_path / "echo.yaml"
    write_manifest(manifest_path)
    kubectl_path = _write_fake_kubectl(tmp_path / "kubectl", fail_probe=True)
    output_path = tmp_path / "k8s_status.json"

    exit_code = main(
        [
            "k8s-report",
            "--samples",
            str(manifest_path),
            "--run-dry-run",
            "--kubectl-bin",
            str(kubectl_path),
            "--kubeconform-bin",
            "/nonexistent",
            "-o",
            str(output_path),
        ]
    )

    assert exit_code == 1
    output = capsys.readouterr().out
    assert "unable to reach a Kubernetes API" in output
    assert "--kubeconfig <path>" in output
    assert not output_path.exists()


def test_k8s_report_uses_explicit_kubeconfig_for_kubectl(tmp_path, monkeypatch):
    log_path = tmp_path / "kubectl.log"
    monkeypatch.setenv("AE_TEST_KUBECTL_LOG", str(log_path))
    kubectl_path = _write_fake_kubectl(tmp_path / "kubectl")
    kubeconfig_path = tmp_path / "kind.kubeconfig"
    kubeconfig_path.write_text("apiVersion: v1\nkind: Config\n")
    output_path = tmp_path / "k8s_status.json"

    exit_code = main(
        [
            "k8s-report",
            "--samples",
            "specs/examples/echo.yaml",
            "--run-dry-run",
            "--kubectl-bin",
            str(kubectl_path),
            "--kubeconfig",
            str(kubeconfig_path),
            "--kubeconform-bin",
            "/nonexistent",
            "-o",
            str(output_path),
        ]
    )

    assert exit_code == 0
    report = json.loads(output_path.read_text())
    assert report["overall_score"] > 0
    assert report["results"][0]["server_dry_run"]["ok"] is True

    calls = [json.loads(line) for line in log_path.read_text().splitlines()]
    assert calls
    assert all(call["kubeconfig"] == str(kubeconfig_path) for call in calls)
    assert any("apply" in call["argv"] for call in calls)
