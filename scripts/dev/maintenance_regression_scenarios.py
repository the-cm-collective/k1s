#!/usr/bin/env python3
"""Run focused Maintenance Notes regression scenarios.

The scenarios are intentionally deterministic and local. WorkerBee can execute this
script inside a checked-out tree as an integration gate after deploying/running the
project profile.
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml

import ae.storage.controller as storage_controller
from ae.apishim.adapter import AdapterWorker
from ae.apishim.store import K8sObject, ObjectStore
from ae.controller.health import HealthManager
from ae.controller.reconciler import Reconciler
from ae.controller.spec import load_manifest
from ae.controller.state import SQLiteStateStore
from ae.runtime import StubRuntime
from ae.storage.controller import StorageController

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures" / "maintenance_regressions"


@dataclass(slots=True)
class ScenarioResult:
    name: str
    ok: bool
    detail: str


@contextmanager
def _env(**updates: str):
    old = {key: os.environ.get(key) for key in updates}
    try:
        for key, value in updates.items():
            os.environ[key] = value
        yield
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _load_yaml(name: str) -> dict[str, Any]:
    data = yaml.safe_load((FIXTURES / name).read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def _k8s_object(name: str, *, resource: str) -> K8sObject:
    data = _load_yaml(name)
    api_version = str(data.get("apiVersion") or "v1")
    if "/" in api_version:
        group, version = api_version.split("/", 1)
    else:
        group, version = "", api_version
    metadata = data.get("metadata") or {}
    spec = data.get("spec") or {}
    return K8sObject(
        group,
        version,
        resource,
        metadata.get("namespace"),
        metadata.get("name"),
        metadata,
        spec,
        {},
        1,
    )


def _adapter(tmp: Path) -> tuple[ObjectStore, SQLiteStateStore, AdapterWorker]:
    store = ObjectStore(tmp / "apishim.db")
    state = SQLiteStateStore(tmp / "state.db")
    reconciler = Reconciler(
        runtime=StubRuntime(),
        state_store=state,
        health_manager=HealthManager(),
    )
    return store, state, AdapterWorker(store, state, reconciler)


def scenario_bad_secret_degrades(tmp: Path) -> None:
    runtime = StubRuntime()
    state = SQLiteStateStore(tmp / "state.db")
    manifest = load_manifest(FIXTURES / "bad-secret.yaml")
    report = Reconciler(runtime=runtime, state_store=state).reconcile(manifest)
    if report.revision_status != "degraded":
        raise AssertionError(f"expected degraded, got {report.revision_status}")
    if state.get_status("bad-secret").revision_status != "degraded":
        raise AssertionError("state snapshot did not record degraded")


def scenario_invalid_cronjob_no_job(tmp: Path) -> None:
    store, _state, adapter = _adapter(tmp)
    adapter._apply_cronjob(_k8s_object("invalid-cronjob.yaml", resource="cronjobs"))
    if store.list("batch", "v1", "jobs", "default"):
        raise AssertionError("invalid CronJob created a Job")
    cron = store.get("batch", "v1", "cronjobs", "default", "invalid-cron")
    conditions = {item.get("type"): item for item in (cron.status or {}).get("conditions", [])}
    if conditions.get("ScheduleValid", {}).get("reason") != "InvalidSchedule":
        raise AssertionError("invalid CronJob status did not expose InvalidSchedule")


def _seed_demo_deployment(store: ObjectStore) -> None:
    store.upsert(
        "apps",
        "v1",
        "deployments",
        "default",
        "demo",
        {"name": "demo", "namespace": "default"},
        {
            "selector": {"matchLabels": {"app": "demo"}},
            "template": {
                "metadata": {"labels": {"app": "demo"}},
                "spec": {
                    "containers": [
                        {
                            "name": "demo",
                            "image": "busybox",
                            "ports": [{"name": "http", "containerPort": 8080}],
                        }
                    ]
                },
            },
        },
    )


def scenario_valid_named_targetport(tmp: Path) -> None:
    store, _state, adapter = _adapter(tmp)
    _seed_demo_deployment(store)
    result = adapter._service_spec_for(
        _k8s_object("service-valid-targetport.yaml", resource="services")
    )
    if result is None:
        raise AssertionError("valid named targetPort did not resolve")
    _dep_key, service = result
    if service.ports[0].target_port != 8080:
        raise AssertionError(f"expected target port 8080, got {service.ports[0].target_port}")


def scenario_unresolved_named_targetport_rejected(tmp: Path) -> None:
    store, _state, adapter = _adapter(tmp)
    _seed_demo_deployment(store)
    result = adapter._service_spec_for(
        _k8s_object("service-unresolved-targetport.yaml", resource="services")
    )
    if result is not None:
        raise AssertionError("unresolved named targetPort was accepted")


def scenario_csi_missing_registry_attach_failed(tmp: Path) -> None:
    store = ObjectStore(tmp / "apishim.db")
    controller = StorageController(store)
    pv = _k8s_object("csi-pv-missing-registry.yaml", resource="persistentvolumes")
    store.upsert("", "v1", "persistentvolumes", None, pv.name, pv.metadata, pv.spec)
    store.upsert(
        "",
        "v1",
        "persistentvolumeclaims",
        "default",
        "data",
        {
            "name": "data",
            "namespace": "default",
            "uid": "uid-csi",
            "annotations": {"volume.kubernetes.io/selected-node": "node-a"},
        },
        {
            "accessModes": ["ReadWriteOnce"],
            "volumeName": "pv-csi",
            "resources": {"requests": {"storage": "1Gi"}},
        },
    )
    controller.reconcile_once()
    va_name = controller._volume_attachment_name("pv-csi", "node-a")
    attachment = store.get("storage.k8s.io", "v1", "volumeattachments", None, va_name)
    if attachment is None:
        raise AssertionError("VolumeAttachment was not created")
    if (attachment.status or {}).get("attached") is not False:
        raise AssertionError("missing CSI registry did not fail attachment")


def scenario_csi_registered_attach_succeeds(tmp: Path) -> None:
    with _env(AE_STORAGE_PROVISIONERS=str(FIXTURES / "csi-provisioners.yaml")):
        old_grpc = storage_controller.grpc
        storage_controller.grpc = object()
        old_client = StorageController._csi_controller_client

        class FakeResp:
            publish_context = {"node": "node-a"}

        class FakeClient:
            def controller_publish(self, **_kwargs):  # noqa: ANN003
                return FakeResp()

        def fake_controller_client(_self, _driver, _sc_name):
            return FakeClient()

        StorageController._csi_controller_client = (  # type: ignore[method-assign]
            fake_controller_client
        )
        try:
            store = ObjectStore(tmp / "apishim.db")
            controller = StorageController(store)
            controller.sync()
            pv = _k8s_object("csi-pv-missing-registry.yaml", resource="persistentvolumes")
            pv.spec["storageClassName"] = "csi-fast"
            store.upsert("", "v1", "persistentvolumes", None, pv.name, pv.metadata, pv.spec)
            store.upsert(
                "",
                "v1",
                "persistentvolumeclaims",
                "default",
                "data",
                {
                    "name": "data",
                    "namespace": "default",
                    "uid": "uid-csi",
                    "annotations": {"volume.kubernetes.io/selected-node": "node-a"},
                },
                {
                    "accessModes": ["ReadWriteOnce"],
                    "volumeName": "pv-csi",
                    "resources": {"requests": {"storage": "1Gi"}},
                },
            )
            controller.reconcile_once()
            va_name = controller._volume_attachment_name("pv-csi", "node-a")
            attachment = store.get("storage.k8s.io", "v1", "volumeattachments", None, va_name)
            if attachment is None:
                raise AssertionError("VolumeAttachment was not created")
            if (attachment.status or {}).get("attached") is not True:
                raise AssertionError("registered CSI provisioner did not attach")
        finally:
            StorageController._csi_controller_client = old_client  # type: ignore[method-assign]
            storage_controller.grpc = old_grpc


SCENARIOS: dict[str, Callable[[Path], None]] = {
    "bad-secret-degrades": scenario_bad_secret_degrades,
    "invalid-cronjob-no-job": scenario_invalid_cronjob_no_job,
    "valid-named-targetport": scenario_valid_named_targetport,
    "unresolved-named-targetport-rejected": scenario_unresolved_named_targetport_rejected,
    "csi-missing-registry-attach-failed": scenario_csi_missing_registry_attach_failed,
    "csi-registered-attach-succeeds": scenario_csi_registered_attach_succeeds,
}


def _run_one(name: str, fn: Callable[[Path], None]) -> ScenarioResult:
    with tempfile.TemporaryDirectory(prefix=f"k1s-maint-{name}-") as raw:
        try:
            fn(Path(raw))
        except Exception as exc:  # noqa: BLE001
            return ScenarioResult(name=name, ok=False, detail=str(exc))
    return ScenarioResult(name=name, ok=True, detail="ok")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workerbee",
        action="store_true",
        help="Annotate output as WorkerBee gate",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON summary")
    args = parser.parse_args(argv)

    results = [_run_one(name, fn) for name, fn in SCENARIOS.items()]
    payload = {
        "workerbee": bool(args.workerbee),
        "ok": all(result.ok for result in results),
        "results": [asdict(result) for result in results],
    }
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        for result in results:
            status = "PASS" if result.ok else "FAIL"
            print(f"{status} {result.name}: {result.detail}")
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
