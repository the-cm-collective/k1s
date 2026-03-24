from __future__ import annotations

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CNI_INIT_SCRIPT = ROOT / "scripts" / "cni_init.sh"
CRI_SMOKE_SCRIPT = ROOT / "scripts" / "cri_smoke.sh"
CRI_CI_SETUP_SCRIPT = ROOT / "scripts" / "cri_ci_setup.sh"
COMMON_BOOTSTRAP_SCRIPT = ROOT / "lab" / "packer" / "http" / "common-bootstrap.sh"
GUEST_PREREQS_SCRIPT = ROOT / "scripts" / "lab" / "vm" / "lib" / "guest_prereqs.sh"
IMAGE_BUILD_SCRIPT = ROOT / "scripts" / "lab" / "vm" / "image_build.sh"
IMAGE_VERIFY_SCRIPT = ROOT / "scripts" / "lab" / "vm" / "image_verify.sh"


def test_cni_init_defaults_to_compatible_version(tmp_path: Path) -> None:
    conf_dir = tmp_path / "cni"
    conf_dir.mkdir()

    env = os.environ.copy()
    env["CNI_CONF_DIR"] = str(conf_dir)
    env["AE_CNI_FORCE"] = "1"

    subprocess.run(
        ["bash", str(CNI_INIT_SCRIPT)],
        check=True,
        text=True,
        capture_output=True,
        env=env,
    )

    bridge = (conf_dir / "10-k1s-bridge.conflist").read_text(encoding="utf-8")
    loopback = (conf_dir / "99-loopback.conf").read_text(encoding="utf-8")

    assert '"cniVersion": "0.4.0"' in bridge
    assert '"type": "firewall"' in bridge
    assert '"cniVersion": "0.4.0"' in loopback


def test_cni_init_honors_version_override(tmp_path: Path) -> None:
    conf_dir = tmp_path / "cni"
    conf_dir.mkdir()

    env = os.environ.copy()
    env["CNI_CONF_DIR"] = str(conf_dir)
    env["AE_CNI_FORCE"] = "1"
    env["AE_CNI_VERSION"] = "1.0.0"

    subprocess.run(
        ["bash", str(CNI_INIT_SCRIPT)],
        check=True,
        text=True,
        capture_output=True,
        env=env,
    )

    bridge = (conf_dir / "10-k1s-bridge.conflist").read_text(encoding="utf-8")
    assert '"cniVersion": "1.0.0"' in bridge


def test_cri_smoke_runs_podsandbox_and_cleans_up(tmp_path: Path) -> None:
    log_path = tmp_path / "crictl.log"
    pod_cfg_copy = tmp_path / "pod.json"
    fake_crictl = tmp_path / "crictl"
    fake_crictl.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >>"$FAKE_CRICTL_LOG"
args=("$@")
cmd=""
rest=()
for ((i=0; i<${#args[@]}; i++)); do
  if [[ "${args[i]}" == "--runtime-endpoint" ]]; then
    ((i+=1))
    continue
  fi
  cmd="${args[i]}"
  rest=("${args[@]:i+1}")
  break
done
case "$cmd" in
  info|pull|stopp|rmp)
    exit 0
    ;;
  runp)
    cfg="${rest[${#rest[@]}-1]}"
    cp "$cfg" "$FAKE_POD_JSON"
    printf '%s\\n' "fake-pod-id"
    exit 0
    ;;
  pods)
    exit 0
    ;;
  *)
    echo "unexpected command: $cmd" >&2
    exit 1
    ;;
esac
""",
        encoding="utf-8",
    )
    fake_crictl.chmod(0o755)

    env = os.environ.copy()
    env["CRICTL_BIN"] = str(fake_crictl)
    env["FAKE_CRICTL_LOG"] = str(log_path)
    env["FAKE_POD_JSON"] = str(pod_cfg_copy)
    env["AE_CRI_ENDPOINT"] = "unix:///tmp/fake-containerd.sock"
    env["AE_CRI_RUNTIME_HANDLER"] = "kata"
    env["AE_CRI_SMOKE_NAMESPACE"] = "smoke-ns"
    env["AE_CRI_SMOKE_PULL"] = "0"

    proc = subprocess.run(
        ["bash", str(CRI_SMOKE_SCRIPT)],
        check=True,
        text=True,
        capture_output=True,
        env=env,
    )

    log = log_path.read_text(encoding="utf-8").splitlines()
    pod_cfg = pod_cfg_copy.read_text(encoding="utf-8")

    assert any(" info" in line for line in log)
    assert any(" runp -r kata " in line for line in log)
    assert any(" stopp fake-pod-id" in line for line in log)
    assert any(" rmp fake-pod-id" in line for line in log)
    assert '"namespace": "smoke-ns"' in pod_cfg
    assert "PodSandbox" in proc.stdout


def test_cri_bootstrap_scripts_use_compatible_cni_default() -> None:
    assert 'cni_version="${AE_CNI_VERSION:-0.4.0}"' in CNI_INIT_SCRIPT.read_text(encoding="utf-8")
    assert 'cni_version="${AE_CNI_VERSION:-0.4.0}"' in CRI_CI_SETUP_SCRIPT.read_text(
        encoding="utf-8"
    )
    common_bootstrap = COMMON_BOOTSTRAP_SCRIPT.read_text(encoding="utf-8")
    assert common_bootstrap.count('"cniVersion": "0.4.0"') == 2
    assert "crictl pull registry.k8s.io/pause:3.9" in common_bootstrap
    assert "bootstrap_contract_version" in common_bootstrap
    assert '"cni_version": "${expected_cni_version}"' in common_bootstrap

    image_build = IMAGE_BUILD_SCRIPT.read_text(encoding="utf-8")
    assert 'BOOTSTRAP_CONTRACT_VERSION="${BOOTSTRAP_CONTRACT_VERSION:-20260324-cni-0.4.0-smoke-v1}"' in image_build
    assert 'EXPECTED_CNI_VERSION="${EXPECTED_CNI_VERSION:-0.4.0}"' in image_build
    assert "bootstrap_contract_version:$bootstrap_contract_version" in image_build
    assert "cni_version:$cni_version" in image_build

    image_verify = IMAGE_VERIFY_SCRIPT.read_text(encoding="utf-8")
    assert 'BOOTSTRAP_CONTRACT_VERSION="${BOOTSTRAP_CONTRACT_VERSION:-20260324-cni-0.4.0-smoke-v1}"' in image_verify
    assert 'EXPECTED_CNI_VERSION="${EXPECTED_CNI_VERSION:-0.4.0}"' in image_verify
    assert ".bootstrap_contract_version == $v" in image_verify
    assert ".cni_version == $v" in image_verify


def test_guest_prereqs_runs_cri_sandbox_smoke() -> None:
    text = GUEST_PREREQS_SCRIPT.read_text(encoding="utf-8")
    assert "/mnt/host/scripts/cri_smoke.sh" in text
    assert "AE_CRI_SMOKE_PULL=0" in text
    assert "[vm-prereqs] cri-sandbox-smoke detail:" in text
    assert "tail -n 5" in text
    assert 'missing+=("cri-sandbox-smoke")' in text
