import json
from pathlib import Path

from scripts.bench import label_backfill as LB


def test_normalize_label_rebuilds_k3d_suffix_from_stage() -> None:
    label = "stamp+k3d-idle"
    assert (
        LB._normalize_label(
            label,
            backend="docker",
            oci="runc",
            mode="k3s",
            rootless=False,
            cgroups="cg2",
        )
        == "stamp+k3d+runc-idle"
    )


def test_patch_snapshot_prefers_snapshot_detected_oci_over_host_fallback(tmp_path: Path) -> None:
    snap = tmp_path
    raw = snap / "raw"
    raw.mkdir(parents=True, exist_ok=True)

    meta_path = snap / "meta.json"
    meta_path.write_text(json.dumps({"label": "stamp+docker+k1nd-idle", "backend": "docker"}))
    (raw / "docker_inspect.json").write_text(json.dumps([{"HostConfig": {"Runtime": "runc"}}]))

    calls: list[list[str]] = []

    def fake_run(argv: list[str], check: bool = False) -> None:
        calls.append(argv)

    original_run = LB.subprocess.run
    LB.subprocess.run = fake_run
    try:
        changed = LB.patch_snapshot(
            snap,
            "crun",
            force_oci=False,
            insert_into_label=True,
        )
    finally:
        LB.subprocess.run = original_run

    assert changed is True
    meta = json.loads(meta_path.read_text())
    assert meta["oci_runtime"] == "runc"
    assert meta["label"] == "stamp+docker+runc+k1nd-idle"
    assert calls == [["python", "scripts/bench/mem_aggregate.py", str(snap)]]


def test_patch_snapshot_normalizes_corrupted_k3d_label(tmp_path: Path) -> None:
    snap = tmp_path
    raw = snap / "raw"
    raw.mkdir(parents=True, exist_ok=True)

    meta_path = snap / "meta.json"
    meta_path.write_text(
        json.dumps(
            {
                "label": "stamp+k3d+crun++runc-pods-5",
                "backend": "docker",
                "mode": "k3s",
                "oci_runtime": "runc",
            }
        )
    )

    calls: list[list[str]] = []

    def fake_run(argv: list[str], check: bool = False) -> None:
        calls.append(argv)

    original_run = LB.subprocess.run
    LB.subprocess.run = fake_run
    try:
        changed = LB.patch_snapshot(
            snap,
            "crun",
            force_oci=False,
            insert_into_label=True,
        )
    finally:
        LB.subprocess.run = original_run

    assert changed is True
    meta = json.loads(meta_path.read_text())
    assert meta["label"] == "stamp+k3d+runc-pods-5"
    assert calls == [["python", "scripts/bench/mem_aggregate.py", str(snap)]]
