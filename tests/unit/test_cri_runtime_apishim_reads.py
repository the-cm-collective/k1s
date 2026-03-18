import base64

from ae.controller.spec import AppManifest
from ae.k8s.convert import manifest_from_k8s_workload
from ae.k8s.exporter import ExportOptions, _deployment_from_manifest
from ae.runtime.cri_runtime import CRIRuntime
from ae.storage.state import ApishimHttpStorageState


class _FakeResponse:
    def __init__(self, payload: dict | None, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")


def _manifest_dict(*, service_account_name: str | None = None) -> dict:
    spec = {
        "replicas": 1,
        "selector": {"matchLabels": {"app": "demo"}},
        "template": {
            "metadata": {"labels": {"app": "demo"}},
            "spec": {
                "containers": [{"name": "demo", "image": "busybox"}],
            },
        },
    }
    if service_account_name:
        spec["template"]["spec"]["serviceAccountName"] = service_account_name
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": "demo", "namespace": "default"},
        "spec": spec,
    }


def test_apishim_http_storage_state_reads_secret_and_serviceaccount(monkeypatch) -> None:
    monkeypatch.setenv("AE_APISHIM_URL", "https://apishim.example.test")
    monkeypatch.setenv("AE_APISHIM_READ_TOKEN", "read-token")
    monkeypatch.setenv("AE_APISHIM_CA_BUNDLE", "/tmp/fake-ca.pem")

    calls: list[tuple[str, str | None, object]] = []
    encoded = base64.b64encode(b"user:pass").decode("ascii")

    def fake_get(url, *, headers=None, timeout=None, verify=None):
        calls.append((url, (headers or {}).get("Authorization"), verify))
        if url.endswith("/api/v1/namespaces/default/secrets/regcred"):
            return _FakeResponse({"data": {".dockerconfigjson": encoded}})
        if url.endswith("/api/v1/namespaces/default/serviceaccounts/demo-sa"):
            return _FakeResponse(
                {
                    "apiVersion": "v1",
                    "kind": "ServiceAccount",
                    "metadata": {"name": "demo-sa", "namespace": "default"},
                    "imagePullSecrets": [{"name": "regcred"}],
                }
            )
        return _FakeResponse(None, status_code=404)

    monkeypatch.setattr("ae.storage.state.requests.get", fake_get)

    state = ApishimHttpStorageState.from_env()
    assert state is not None

    secret = state.get_secret("default", "regcred")
    service_account = state.get_service_account("default", "demo-sa")

    assert secret == {".dockerconfigjson": "user:pass"}
    assert service_account == {"imagePullSecrets": [{"name": "regcred"}]}
    assert calls[0][1] == "Bearer read-token"
    assert calls[0][2] == "/tmp/fake-ca.pem"


def test_cri_runtime_ha_prefers_http_serviceaccount_reads(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AE_HA_MODE", "1")
    monkeypatch.setenv("AE_APISHIM_DB", str(tmp_path / "apishim.db"))
    monkeypatch.setenv("AE_APISHIM_URL", "http://apishim.example.test")
    monkeypatch.setenv("AE_APISHIM_READ_TOKEN", "read-token")

    from ae.apishim.store import ObjectStore

    legacy = ObjectStore(tmp_path / "apishim.db")
    legacy.upsert(
        "",
        "v1",
        "serviceaccounts",
        "default",
        "demo-sa",
        metadata={"name": "demo-sa", "namespace": "default"},
        spec={"imagePullSecrets": [{"name": "legacy-regcred"}]},
        status={},
    )

    def fake_get(url, *, headers=None, timeout=None, verify=None):
        _ = (headers, timeout, verify)
        if url.endswith("/api/v1/namespaces/default/serviceaccounts/demo-sa"):
            return _FakeResponse(
                {
                    "apiVersion": "v1",
                    "kind": "ServiceAccount",
                    "metadata": {"name": "demo-sa", "namespace": "default"},
                    "imagePullSecrets": [{"name": "remote-regcred"}],
                }
            )
        return _FakeResponse(None, status_code=404)

    monkeypatch.setattr("ae.storage.state.requests.get", fake_get)

    runtime = CRIRuntime()
    manifest = AppManifest.model_validate(
        {
            "apiVersion": "ae.dev/v1alpha1",
            "kind": "Deployment",
            "metadata": {"name": "demo", "namespace": "default"},
            "spec": {
                "image": "busybox",
                "serviceAccountName": "demo-sa",
            },
        }
    )

    secrets = runtime._service_account_pull_secrets(manifest)

    assert secrets == [("remote-regcred", None)]


def test_manifest_and_export_preserve_service_account_name() -> None:
    manifest = manifest_from_k8s_workload(_manifest_dict(service_account_name="demo-sa"))

    assert manifest.spec.service_account_name == "demo-sa"

    doc = _deployment_from_manifest(manifest, ExportOptions(namespace="default"))

    assert doc["spec"]["template"]["spec"]["serviceAccountName"] == "demo-sa"
