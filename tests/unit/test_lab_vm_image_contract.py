from __future__ import annotations

import json
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
VARIANT_PATH = ROOT / "scripts" / "lab" / "vm" / "lib" / "variant.py"
SPEC = spec_from_file_location("lab_vm_variant", VARIANT_PATH)
assert SPEC is not None and SPEC.loader is not None
variant = module_from_spec(SPEC)
SPEC.loader.exec_module(variant)


def _write_meta(path: Path, *, contract: str, cni_version: str) -> None:
    Path(f"{path}.meta.json").write_text(
        json.dumps(
            {
                "vm_bootstrap_ready": True,
                "python_alias": True,
                "crictl_ready": True,
                "cni_ready": True,
                "bootstrap_contract_version": contract,
                "cni_version": cni_version,
            }
        ),
        encoding="utf-8",
    )


def _write_variant(path: Path, *, base: Path, gpu: Path) -> None:
    path.write_text(
        f"""
name: test-variant
test_id: 1
network:
  bridge: k1s-br0
  cidr: 192.168.155.0/24
  gateway: 192.168.155.1
vm:
  memory_mb: 4096
  vcpus: 2
  disk_gb: 30
images:
  base: {base}
  gpu: {gpu}
hosts:
  - name: core-a
    ip: 192.168.155.10
    role: k1s-core
    gpu: false
""".strip(),
        encoding="utf-8",
    )


def test_parse_variant_validate_images_accepts_matching_contract(tmp_path: Path) -> None:
    base = tmp_path / "base.qcow2"
    gpu = tmp_path / "gpu.qcow2"
    base.write_bytes(b"")
    gpu.write_bytes(b"")
    _write_meta(
        base,
        contract=variant.BOOTSTRAP_CONTRACT_VERSION,
        cni_version=variant.EXPECTED_CNI_VERSION,
    )
    _write_meta(
        gpu,
        contract=variant.BOOTSTRAP_CONTRACT_VERSION,
        cni_version=variant.EXPECTED_CNI_VERSION,
    )
    variant_file = tmp_path / "variant.yaml"
    _write_variant(variant_file, base=base, gpu=gpu)

    payload = variant.parse_variant(variant_file, validate_images=True)

    assert payload["images"]["base"] == str(base.resolve())
    assert payload["images"]["gpu"] == str(gpu.resolve())


def test_parse_variant_validate_images_rejects_stale_contract(tmp_path: Path) -> None:
    base = tmp_path / "base.qcow2"
    gpu = tmp_path / "gpu.qcow2"
    base.write_bytes(b"")
    gpu.write_bytes(b"")
    _write_meta(base, contract="old-contract", cni_version="1.0.0")
    _write_meta(
        gpu,
        contract=variant.BOOTSTRAP_CONTRACT_VERSION,
        cni_version=variant.EXPECTED_CNI_VERSION,
    )
    variant_file = tmp_path / "variant.yaml"
    _write_variant(variant_file, base=base, gpu=gpu)

    try:
        variant.parse_variant(variant_file, validate_images=True)
    except ValueError as exc:
        message = str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected parse_variant to reject stale image metadata")

    assert "bootstrap_contract_version" in message or "cni_version" in message
