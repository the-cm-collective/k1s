from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "bench" / "wait_rollout_steady.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("wait_rollout_steady", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_parse_container_sample_accepts_single_running_revision_from_docker_inspect_shape() -> None:
    module = _load_module()

    sample = module.parse_container_sample(
        "echo",
        [
            {
                "Name": "/ae-echo-rev7-0",
                "Config": {"Labels": {"ae.app": "echo"}},
                "State": {"Status": "running"},
            }
        ],
    )

    assert sample.app_present is True
    assert sample.revisions == ("rev7",)
    assert sample.orphan_count == 0
    assert sample.is_steady is True


def test_parse_container_sample_marks_multiple_revisions_and_exited_container_unsteady() -> None:
    module = _load_module()

    sample = module.parse_container_sample(
        "echo",
        [
            {
                "Name": "/ae-echo-rev1-0",
                "Config": {"Labels": {"ae.app": "echo", "ae.revision": "rev1"}},
                "State": {"Status": "running"},
            },
            {
                "Name": "/ae-echo-rev2-0",
                "Config": {"Labels": {"ae.app": "echo", "ae.revision": "rev2"}},
                "State": {"Status": "exited"},
            },
        ],
    )

    assert sample.app_present is True
    assert sample.revisions == ("rev1", "rev2")
    assert sample.orphan_count == 1
    assert sample.is_steady is False


def test_parse_cri_sample_flags_orphan_containers_outside_live_pods() -> None:
    module = _load_module()

    sample = module.parse_cri_sample(
        "echo",
        {
            "items": [
                {
                    "id": "pod-live",
                    "metadata": {"name": "echo-rev3-0"},
                    "labels": {"ae.app": "echo", "ae.revision": "rev3"},
                }
            ]
        },
        {
            "containers": [
                {
                    "id": "cid-live",
                    "podSandboxId": "pod-live",
                    "labels": {
                        "ae.app": "echo",
                        "ae.pod_name": "echo-rev3-0",
                        "ae.revision": "rev3",
                    },
                },
                {
                    "id": "cid-orphan",
                    "podSandboxId": "pod-stale",
                    "labels": {
                        "ae.app": "echo",
                        "ae.pod_name": "echo-rev2-0",
                        "ae.revision": "rev2",
                    },
                },
            ]
        },
    )

    assert sample.app_present is True
    assert sample.live_container_count == 2
    assert sample.revisions == ("rev2", "rev3")
    assert sample.orphan_count == 1
    assert sample.is_steady is False


def test_parse_container_sample_handles_empty_app_state() -> None:
    module = _load_module()

    sample = module.parse_container_sample("echo", [])

    assert sample.app_present is False
    assert sample.revisions == ()
    assert sample.orphan_count == 0
    assert sample.is_steady is False
