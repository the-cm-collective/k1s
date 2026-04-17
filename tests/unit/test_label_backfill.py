import json
from pathlib import Path
from types import SimpleNamespace

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


def test_normalize_label_preserves_cri_run_prefix_when_inserting_oci() -> None:
    label = "stamp+cri-runc-verify-run2+cri+containerd-pods-10"
    assert (
        LB._normalize_label(
            label,
            backend="cri",
            oci="runc",
            mode="k1s",
            rootless=False,
            cgroups="cg2",
        )
        == "stamp+cri-runc-verify-run2+cri+runc+containerd-pods-10"
    )


def test_patch_snapshot_prefers_snapshot_detected_oci_over_host_fallback(tmp_path: Path) -> None:
    snap = tmp_path
    raw = snap / "raw"
    raw.mkdir(parents=True, exist_ok=True)

    meta_path = snap / "meta.json"
    meta_path.write_text(json.dumps({"label": "stamp+docker+k1nd-idle", "backend": "docker"}))
    (raw / "docker_inspect.json").write_text(json.dumps([{"HostConfig": {"Runtime": "runc"}}]))

    calls: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: object) -> SimpleNamespace:
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

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

    def fake_run(argv: list[str], **kwargs: object) -> SimpleNamespace:
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

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


def test_patch_snapshot_restores_label_from_snapshot_dir_for_cri_without_host_guess(
    tmp_path: Path,
) -> None:
    snap = (
        tmp_path / "stamp+cri-runc-verify-run2+cri+containerd-pods-10" / "20260416-123456"
    )
    snap.mkdir(parents=True, exist_ok=True)

    meta_path = snap / "meta.json"
    meta_path.write_text(
        json.dumps(
            {
                "label": "stamp+cri+crun+containerd-pods-10",
                "backend": "cri",
                "mode": "k1s",
            }
        )
    )

    calls: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: object) -> SimpleNamespace:
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

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
    assert meta["label"] == "stamp+cri-runc-verify-run2+cri+containerd-pods-10"
    assert "oci_runtime" not in meta
    assert calls == [["python", "scripts/bench/mem_aggregate.py", str(snap)]]


def test_patch_snapshot_force_oci_repairs_cri_run_label_from_snapshot_dir(tmp_path: Path) -> None:
    snap = (
        tmp_path / "stamp+cri-runc-verify-run2+cri+containerd-pods-10" / "20260416-123456"
    )
    snap.mkdir(parents=True, exist_ok=True)

    meta_path = snap / "meta.json"
    meta_path.write_text(
        json.dumps(
            {
                "label": "stamp+cri+crun+containerd-pods-10",
                "backend": "cri",
            }
        )
    )

    calls: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: object) -> SimpleNamespace:
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    original_run = LB.subprocess.run
    LB.subprocess.run = fake_run
    try:
        changed = LB.patch_snapshot(
            snap,
            "runc",
            force_oci=True,
            insert_into_label=True,
        )
    finally:
        LB.subprocess.run = original_run

    assert changed is True
    meta = json.loads(meta_path.read_text())
    assert meta["label"] == "stamp+cri-runc-verify-run2+cri+runc+containerd-pods-10"
    assert meta["oci_runtime"] == "runc"
    assert calls == [["python", "scripts/bench/mem_aggregate.py", str(snap)]]


def test_reaggregate_snapshot_is_quiet_on_success(tmp_path: Path, capsys) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(argv: list[str], **kwargs: object) -> SimpleNamespace:
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout='{"meta":"noisy"}\n', stderr="")

    original_run = LB.subprocess.run
    LB.subprocess.run = fake_run
    try:
        LB._reaggregate_snapshot(tmp_path)
    finally:
        LB.subprocess.run = original_run

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
    assert calls == [
        (
            ["python", "scripts/bench/mem_aggregate.py", str(tmp_path)],
            {"check": False, "capture_output": True, "text": True},
        )
    ]


def test_summary_message_avoids_host_backend_noise() -> None:
    assert LB._summary_message(16, oci="runc", force_oci=True) == (
        "patched 16 snapshots (forced_oci=runc)"
    )
    assert LB._summary_message(0, oci="crun", force_oci=False) == "patched 0 snapshots"
