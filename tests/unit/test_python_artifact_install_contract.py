from __future__ import annotations

import ast
import base64
import csv
import hashlib
import io
import os
import tarfile
import zipfile
from pathlib import Path

import pytest
from scripts.check_public_markers import scan_archive
from scripts.validate_installed_python_artifact import (
    offline_environment,
    require_outside_checkout,
    verify_wheelhouse,
)
from scripts.validate_python_artifacts import (
    PROFILE_CATALOG_MEMBER,
    SDIST_SOURCE_ONLY_FILES,
    SDIST_SOURCE_ONLY_IDENTITIES,
    WORKERBEE_ADAPTER_MEMBER,
    _require_unique_names,
    _source_payloads,
    _validate_exact_source_identity,
    _validate_profile_catalog_bytes,
    _validate_wheel_record,
    _validate_workerbee_adapter_bytes,
    _validated_member_name,
)

ROOT = Path(__file__).resolve().parents[2]


def test_release_wheel_lock_is_complete_and_content_addressed() -> None:
    lines = [
        line
        for line in (ROOT / "requirements-release.lock").read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    ]
    assert len(lines) == 24
    assert all(
        " --hash=sha256:" in line and "  # " in line and line.endswith(".whl") for line in lines
    )
    assert any(line.startswith("nkeys==0.2.1 ") for line in lines)
    assert not any(line.lower().startswith("k1s-core") for line in lines)


def test_wheelhouse_verification_rejects_missing_extra_and_changed_wheels(tmp_path: Path) -> None:
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    wheel = wheelhouse / "example-1.0-py3-none-any.whl"
    wheel.write_bytes(b"reviewed wheel")
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    lock = tmp_path / "requirements-release.lock"
    lock.write_text(
        f"example==1.0 --hash=sha256:{digest}  # {wheel.name}\n",
        encoding="utf-8",
    )
    verify_wheelhouse(wheelhouse, lock)

    wheel.write_bytes(b"changed")
    with pytest.raises(ValueError, match="changed"):
        verify_wheelhouse(wheelhouse, lock)


def test_offline_environment_removes_index_proxy_and_checkout_paths(tmp_path: Path) -> None:
    env = offline_environment(
        home=tmp_path / "home",
        temporary=tmp_path / "tmp",
        guard=tmp_path / "guard",
    )
    assert env["PIP_NO_INDEX"] == "1"
    assert env["PIP_CONFIG_FILE"] == os.devnull
    assert env["AE_RUNTIME_BACKEND"] == "stub"
    assert env["PYTHONPATH"] == str(tmp_path / "guard")
    assert not any("proxy" in key.lower() for key in env)
    assert "PIP_INDEX_URL" not in env
    assert "PIP_EXTRA_INDEX_URL" not in env


def test_outside_checkout_guard_accepts_sibling_tree_and_rejects_descendants(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "payload" / "source"
    source_root.mkdir(parents=True)
    sibling_work = tmp_path / "runtime" / "outside-checkout"
    sibling_work.mkdir(parents=True)

    require_outside_checkout(sibling_work, source_root)

    source_work = source_root / "qualification" / "outside-checkout"
    source_work.mkdir(parents=True)
    with pytest.raises(ValueError, match="unexpectedly ran inside the checkout"):
        require_outside_checkout(source_work, source_root)

    with pytest.raises(ValueError, match="unexpectedly ran inside the checkout"):
        require_outside_checkout(source_root, source_root)


def test_archive_scanner_rejects_private_markers_in_wheel_and_sdist(tmp_path: Path) -> None:
    wheel = tmp_path / "k1s_core-0.1.8a1-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("ae/leak.py", 'SOURCE = "/home/alice/git/private"\n')
    assert {finding.rule for finding in scan_archive(wheel)} == {"private-home"}

    payload = tmp_path / "README.md"
    payload.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\n", encoding="utf-8")
    sdist = tmp_path / "k1s_core-0.1.8a1.tar.gz"
    with tarfile.open(sdist, "w:gz") as archive:
        archive.add(payload, arcname="k1s_core-0.1.8a1/README.md")
    assert {finding.rule for finding in scan_archive(sdist)} == {"credential-material"}


@pytest.mark.parametrize(
    "name",
    [
        "",
        "/absolute",
        "../escape",
        "root/../escape",
        "root/./file",
        "root//file",
        "root\\file",
        "C:/absolute",
        "root/\x00file",
    ],
)
def test_artifact_member_paths_fail_closed(name: str) -> None:
    with pytest.raises(ValueError, match="archive member path"):
        _validated_member_name(name)


def test_artifact_members_must_be_unique(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="duplicate members"):
        _require_unique_names(["ae/__init__.py", "ae/__init__.py"], artifact=tmp_path / "a.whl")


def _record_row(name: str, payload: bytes) -> list[str]:
    digest = base64.urlsafe_b64encode(hashlib.sha256(payload).digest()).rstrip(b"=").decode()
    return [name, f"sha256={digest}", str(len(payload))]


def _write_record_wheel(path: Path, *, corrupt_hash: bool = False) -> tuple[list[str], str]:
    member = "ae/example.py"
    payload = b"VALUE = 1\n"
    dist_info = "k1s_core-0.1.8a1.dist-info"
    record = f"{dist_info}/RECORD"
    rows = [_record_row(member, payload), [record, "", ""]]
    if corrupt_hash:
        rows[0][1] = "sha256=invalid"
    output = io.StringIO()
    csv.writer(output, lineterminator="\n").writerows(rows)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(member, payload)
        archive.writestr(record, output.getvalue())
    return [member, record], dist_info


def test_wheel_record_covers_every_member_with_exact_hash_and_size(tmp_path: Path) -> None:
    wheel = tmp_path / "valid.whl"
    names, dist_info = _write_record_wheel(wheel)
    with zipfile.ZipFile(wheel) as archive:
        _validate_wheel_record(
            archive,
            names=names,
            dist_info=dist_info,
            artifact=wheel,
        )


def test_wheel_record_rejects_changed_hash_or_coverage(tmp_path: Path) -> None:
    wheel = tmp_path / "changed.whl"
    names, dist_info = _write_record_wheel(wheel, corrupt_hash=True)
    with zipfile.ZipFile(wheel) as archive:
        with pytest.raises(ValueError, match="hash/size mismatch"):
            _validate_wheel_record(
                archive,
                names=names,
                dist_info=dist_info,
                artifact=wheel,
            )
        with pytest.raises(ValueError, match="coverage mismatch"):
            _validate_wheel_record(
                archive,
                names=[*names, "ae/missing.py"],
                dist_info=dist_info,
                artifact=wheel,
            )


def test_runtime_payload_inventory_is_exact() -> None:
    payload, runtime_data = _source_payloads(ROOT / "pyproject.toml")
    assert len(runtime_data) == 87
    assert len([name for name in runtime_data if name.startswith("ae/resources/sql/")]) == 77
    assert len([name for name in runtime_data if name.endswith(".html")]) == 4
    assert len([name for name in runtime_data if name.endswith(".txt")]) == 1
    assert {name for name in runtime_data if name.endswith(".json")} == {
        PROFILE_CATALOG_MEMBER,
        WORKERBEE_ADAPTER_MEMBER,
    }
    assert {
        name for name in runtime_data if name.startswith("ae/observability/static/vendor/")
    } == {
        "ae/observability/static/vendor/xterm-addon-fit.js",
        "ae/observability/static/vendor/xterm.css",
        "ae/observability/static/vendor/xterm.js",
    }
    assert not any(name.endswith(".md") for name in runtime_data)
    assert "ae/runtime/cri/api/runtime/v1/gogo_pb2.py" in payload
    assert "ae/storage/csi/api/csi_pb2_grpc.py" in payload
    assert {"tests/test_apishim_env.py"} == SDIST_SOURCE_ONLY_FILES
    assert SDIST_SOURCE_ONLY_IDENTITIES == {
        "tests/test_apishim_env.py": (
            823,
            "650f6d4806e5b08f590d1da71ed0b9b38d73c8f05c7945f07e6e4a716d998bbb",
            "2469965183ea9d90e9c0c5c2ba305ccf0068f9d4",
        )
    }
    assert payload.isdisjoint(SDIST_SOURCE_ONLY_FILES)


def test_profile_catalog_artifact_member_is_canonical_and_fail_closed() -> None:
    resource = ROOT / "src" / PROFILE_CATALOG_MEMBER
    payload = resource.read_bytes()

    _validate_profile_catalog_bytes(payload, artifact=resource)
    with pytest.raises(ValueError, match="profile catalog"):
        changed = payload.replace(b'"mutating": false', b'"mutating": true')
        _validate_profile_catalog_bytes(changed, artifact=resource)


def test_workerbee_adapter_artifact_member_is_canonical_and_fail_closed() -> None:
    resource = ROOT / "src" / WORKERBEE_ADAPTER_MEMBER
    payload = resource.read_bytes()

    _validate_workerbee_adapter_bytes(payload, artifact=resource)
    with pytest.raises(ValueError, match="WorkerBee adapter"):
        changed = payload.replace(
            b'"qualificationPosture": "source-validated/unqualified"',
            b'"qualificationPosture": "live-qualified"',
        )
        _validate_workerbee_adapter_bytes(changed, artifact=resource)


def test_installed_artifact_gate_exercises_static_profile_commands() -> None:
    source = (ROOT / "scripts" / "validate_installed_python_artifact.py").read_text(
        encoding="utf-8"
    )

    assert "profiles/profile-catalog-v1.json" in source
    assert "profiles/workerbee-adapter-v1.json" in source
    assert "adapt_workerbee_profile_evidence" in source
    assert "verify_workerbee_profile_evidence" in source
    assert "installed adapter accepted a tampered evidence identity" in source
    for operation in ("list", "doctor", "plan", "status", "logs", "bootstrap"):
        assert f"'{operation}':" in source
    assert '"profile", "list", "--output", "json"' in source
    assert '"profile", "plan", profile_name, "--output", "json"' in source
    assert '"profile", "doctor", profile_name, "--output", "json"' in source
    assert 'plan.get("mutating") is not False' in source

    tree = ast.parse(source)
    smoke_programs = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and "profile_catalog_payload" in node.value
    ]
    assert len(smoke_programs) == 1
    compile(smoke_programs[0], "<installed-artifact-profile-smoke>", "exec")


def test_installed_artifact_gate_exercises_pure_strict_cri_host_plan() -> None:
    source = (ROOT / "scripts" / "validate_installed_python_artifact.py").read_text(
        encoding="utf-8"
    )

    assert "from ae.kctl.profile_host_plan import PLAN_KIND" in source
    assert '"host",\n            "plan",\n            "strict-cri"' in source
    assert '"--expect-intent-sha256"' in source
    assert '"--expect-facts-sha256"' in source
    assert 'host_plan.get("mutating") is not False' in source
    assert 'host_plan.get("qualificationPosture") != "source-validated/unqualified"' in source
    assert "installed strict-CRI host plan mutated its exact inputs" in source
    assert "installed strict-CRI host plan disclosed an authority path" in source
    assert "socket.create_connection = _deny" in source
    assert '"PIP_NO_INDEX": "1"' in source


def test_installed_artifact_gate_exercises_synthetic_strict_cri_transaction() -> None:
    source = (ROOT / "scripts" / "validate_installed_python_artifact.py").read_text(
        encoding="utf-8"
    )

    assert "installed_transaction_program = r" in source
    assert "StrictCRIHostHelperEngine" in source
    assert "PipeHostHelperClient" in source
    assert "StrictCRIHostTransactionManager" in source
    assert "StrictCRINodeAdapter" in source
    assert "request_read, request_write = os.pipe()" in source
    assert "response_read, response_write = os.pipe()" in source
    assert "transaction_root_fd" in source
    assert "assert prepare.status == 'prepared'" in source
    assert "assert rollback.status == 'rolled-back'" in source
    assert "assert public['qualificationPosture'] == 'source-validated/unqualified'" in source
    assert "assert not server_errors" in source
    assert 'rendered[program_index] = "<bounded-installed-smoke-program>"' in source
    assert "subprocess.Popen" not in source
    assert "sudo" not in source

    tree = ast.parse(source)
    smoke_programs = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and "K1S_STRICT_TRANSACTION_SMOKE_ROOT" in node.value
        and "StrictCRIHostTransactionManager" in node.value
    ]
    assert len(smoke_programs) == 1
    compile(smoke_programs[0], "<installed-artifact-strict-cri-transaction>", "exec")


def test_installed_artifact_gate_exercises_state_contract_without_runtime() -> None:
    source = (ROOT / "scripts" / "validate_installed_python_artifact.py").read_text(
        encoding="utf-8"
    )

    assert "from ae.kctl.profile_paths import ProfileRoots" in source
    assert "from ae.kctl.profile_backend import build_execution_plan" in source
    assert "from ae.kctl.profile_bootstrap import ProfileBootstrapError" in source
    assert "from ae.kctl.profile_engine import EngineCapability" in source
    assert "from ae.kctl.profile_state import FailureRecord, ProfileStateStore" in source
    assert "from ae.kctl.profile_supervisor import classify_identity, parse_proc_stat" in source
    assert "K1S_PROFILE_SMOKE_ROOT" in source
    assert "assert store.inspect() is None" in source
    assert "assert not roots.state.exists()" in source
    assert "store.cleanup_owned" in source
    assert "portable_plan_digest('local', synthetic_engine)" in source
    assert "assert plan.digest == plan_digest" in source
    assert 'portable_names = {"local", "core", "core-edge", "edge-gateway", "node"}' in source
    assert 'expected_status = "portable" if profile_name in portable_names' in source
    assert '!= "source-validated/unqualified"' in source
    assert "ProfileSupervisor.system" not in source
    assert "SubprocessLauncher(" not in source
    assert "SelectedEngineProbe.system" not in source
    assert "PortableProfileManager.system" not in source
    assert "ProfileBootstrapStore(roots, 'node')" in source
    assert "ProfileBootstrapStore(roots, 'edge-gateway')" in source
    assert "private bootstrap drift was not rejected" in source
    assert "assert gateway_plan.engine is None" in source
    assert "assert node_private not in repr(node_plan)" in source


def test_sdist_only_source_identity_is_frozen() -> None:
    name = "tests/test_apishim_env.py"
    identity = SDIST_SOURCE_ONLY_IDENTITIES[name]
    payload = (ROOT / name).read_bytes()
    _validate_exact_source_identity(
        payload,
        name=name,
        expected_size=identity[0],
        expected_sha256=identity[1],
        expected_git_blob=identity[2],
    )
    with pytest.raises(ValueError, match="sdist-only source identity mismatch"):
        _validate_exact_source_identity(
            payload + b"\n",
            name=name,
            expected_size=identity[0],
            expected_sha256=identity[1],
            expected_git_blob=identity[2],
        )
