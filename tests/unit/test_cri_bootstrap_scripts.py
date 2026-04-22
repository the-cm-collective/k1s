from __future__ import annotations

import os
import socket
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CNI_INIT_SCRIPT = ROOT / "scripts" / "cni_init.sh"
CNI_BIN_BOOTSTRAP_SCRIPT = ROOT / "scripts" / "cni_bin_bootstrap.sh"
CRI_PREFLIGHT_SCRIPT = ROOT / "scripts" / "cri_preflight.sh"
CRI_IMAGE_MIRROR_SCRIPT = ROOT / "scripts" / "dev" / "cri_image_mirror.sh"
BUILD_CRI_APISHIM_IMAGE_SCRIPT = ROOT / "scripts" / "build_cri_apishim_image.sh"
PROFILE_STATE_OWNERSHIP_SCRIPT = ROOT / "scripts" / "dev" / "profile_state_ownership.sh"
CRI_SMOKE_SCRIPT = ROOT / "scripts" / "cri_smoke.sh"
CRI_CI_SETUP_SCRIPT = ROOT / "scripts" / "cri_ci_setup.sh"
COMMON_BOOTSTRAP_SCRIPT = ROOT / "lab" / "packer" / "http" / "common-bootstrap.sh"
GPU_BOOTSTRAP_SCRIPT = ROOT / "lab" / "packer" / "http" / "gpu-bootstrap.sh"
PACKER_TEMPLATE = ROOT / "lab" / "packer" / "ubuntu-22.04-ga.pkr.hcl"
GUEST_PREREQS_SCRIPT = ROOT / "scripts" / "lab" / "vm" / "lib" / "guest_prereqs.sh"
IMAGE_BUILD_SCRIPT = ROOT / "scripts" / "lab" / "vm" / "image_build.sh"
IMAGE_VERIFY_SCRIPT = ROOT / "scripts" / "lab" / "vm" / "image_verify.sh"

REQUIRED_CNI_PLUGINS = ("bridge", "portmap", "firewall", "tuning", "loopback")


def _write_fake_plugin(path: Path) -> None:
    path.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)


def _write_executable(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


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


def test_cni_init_skips_existing_configs_without_rewriting(tmp_path: Path) -> None:
    conf_dir = tmp_path / "cni"
    conf_dir.mkdir()
    bridge = conf_dir / "10-k1s-bridge.conflist"
    loopback = conf_dir / "99-loopback.conf"
    bridge.write_text('{"name":"existing-bridge"}\n', encoding="utf-8")
    loopback.write_text('{"name":"lo","type":"loopback"}\n', encoding="utf-8")
    bridge.chmod(0o444)
    loopback.chmod(0o444)

    env = os.environ.copy()
    env["CNI_CONF_DIR"] = str(conf_dir)

    proc = subprocess.run(
        ["bash", str(CNI_INIT_SCRIPT)],
        check=True,
        text=True,
        capture_output=True,
        env=env,
    )

    assert bridge.read_text(encoding="utf-8") == '{"name":"existing-bridge"}\n'
    assert loopback.read_text(encoding="utf-8") == '{"name":"lo","type":"loopback"}\n'
    assert "Existing non-loopback CNI config found; skipping bridge config" in proc.stdout
    assert "Loopback config already present" in proc.stdout


def test_cni_bin_bootstrap_populates_destination_from_source_dirs(tmp_path: Path) -> None:
    source_dir = tmp_path / "source-cni"
    dest_dir = tmp_path / "dest-cni"
    source_dir.mkdir()

    for plugin in REQUIRED_CNI_PLUGINS:
        _write_fake_plugin(source_dir / plugin)

    env = os.environ.copy()
    env["CNI_BIN_DIR"] = str(dest_dir)
    env["AE_CNI_BOOTSTRAP_SOURCE_DIRS"] = str(source_dir)

    proc = subprocess.run(
        ["bash", str(CNI_BIN_BOOTSTRAP_SCRIPT)],
        check=True,
        text=True,
        capture_output=True,
        env=env,
    )

    for plugin in REQUIRED_CNI_PLUGINS:
        assert (dest_dir / plugin).exists()
    assert "bootstrapped CNI plugins" in proc.stdout


def test_cni_bin_bootstrap_uses_path_fallback_for_nixos_style_plugins(tmp_path: Path) -> None:
    path_bin = tmp_path / "cni-plugins" / "bin"
    dest_dir = tmp_path / "dest-cni"
    missing_dir = tmp_path / "missing-cni"
    path_bin.mkdir(parents=True)
    missing_dir.mkdir()

    for plugin in REQUIRED_CNI_PLUGINS:
        _write_fake_plugin(path_bin / plugin)

    env = os.environ.copy()
    env["CNI_BIN_DIR"] = str(dest_dir)
    env["AE_CNI_BOOTSTRAP_SOURCE_DIRS"] = str(missing_dir)
    env["PATH"] = f"{path_bin}:{env['PATH']}"

    subprocess.run(
        ["bash", str(CNI_BIN_BOOTSTRAP_SCRIPT)],
        check=True,
        text=True,
        capture_output=True,
        env=env,
    )

    for plugin in REQUIRED_CNI_PLUGINS:
        assert (dest_dir / plugin).exists()


def test_cni_bin_bootstrap_fails_when_required_plugins_are_missing(tmp_path: Path) -> None:
    source_dir = tmp_path / "source-cni"
    dest_dir = tmp_path / "dest-cni"
    source_dir.mkdir()
    _write_fake_plugin(source_dir / "unit-bridge")

    env = os.environ.copy()
    env["CNI_BIN_DIR"] = str(dest_dir)
    env["AE_CNI_BOOTSTRAP_SOURCE_DIRS"] = str(source_dir)
    env["AE_CNI_REQUIRED_PLUGINS"] = "unit-bridge,unit-missing"

    proc = subprocess.run(
        ["bash", str(CNI_BIN_BOOTSTRAP_SCRIPT)],
        check=False,
        text=True,
        capture_output=True,
        env=env,
    )

    assert proc.returncode == 1
    assert "missing required CNI plugins: unit-missing" in proc.stderr


def test_profile_state_ownership_check_passes_for_user_owned_state(tmp_path: Path) -> None:
    profile_dir = tmp_path / "state" / "profiles" / "k1s-core"
    edge_dir = profile_dir / "edge-ingress"
    tls_dir = tmp_path / "state" / "tls"
    edge_dir.mkdir(parents=True)
    tls_dir.mkdir(parents=True)
    (edge_dir / "envoy.yaml").write_text("static_resources: {}\n", encoding="utf-8")
    (tls_dir / "envoy-fallback.crt").write_text("cert\n", encoding="utf-8")
    (tls_dir / "envoy-fallback.key").write_text("key\n", encoding="utf-8")
    (tls_dir / "envoy-fallback.key").chmod(0o600)

    env = os.environ.copy()
    env["K1S_ROOT_DIR_OVERRIDE"] = str(tmp_path)

    proc = subprocess.run(
        ["bash", str(PROFILE_STATE_OWNERSHIP_SCRIPT), "--profile", "k1s-core", "--check"],
        check=False,
        text=True,
        capture_output=True,
        env=env,
    )

    assert proc.returncode == 0


def test_profile_state_ownership_check_fails_for_unwritable_state(tmp_path: Path) -> None:
    lock_dir = tmp_path / "state" / "profiles" / "k1s-core" / "cri" / ".locks"
    lock_dir.mkdir(parents=True)
    (lock_dir.parent).chmod(0o777)
    lock_dir.chmod(0o777)
    lock_file = lock_dir / "k1s-core-etcd.lock"
    lock_file.write_text("held\n", encoding="utf-8")
    lock_file.chmod(0o444)

    env = os.environ.copy()
    env["K1S_ROOT_DIR_OVERRIDE"] = str(tmp_path)

    proc = subprocess.run(
        [
            "bash",
            str(PROFILE_STATE_OWNERSHIP_SCRIPT),
            "--profile",
            "k1s-core",
            "--check",
            "--target-uid",
            "1001",
            "--target-gid",
            "1001",
        ],
        check=False,
        text=True,
        capture_output=True,
        env=env,
    )

    assert proc.returncode == 1
    assert "managed strict-CRI profile state requires repair" in proc.stderr
    assert str(lock_file) in proc.stderr


def test_profile_state_ownership_repair_allows_non_chownable_but_usable_state(tmp_path: Path) -> None:
    profile_dir = tmp_path / "state" / "profiles" / "k1s-core"
    edge_dir = profile_dir / "edge-ingress"
    edge_dir.mkdir(parents=True)
    (edge_dir / "envoy.yaml").write_text("static_resources: {}\n", encoding="utf-8")

    fake_id = tmp_path / "id"
    _write_executable(
        fake_id,
        """#!/usr/bin/env bash
set -euo pipefail
mode="${FAKE_ID_MODE:-root}"
case "${1:-}" in
  -u)
    if [[ "$mode" == "target" ]]; then
      printf '%s\n' "${FAKE_TARGET_UID:-1000}"
    else
      printf '0\n'
    fi
    ;;
  -g)
    if [[ "$mode" == "target" ]]; then
      printf '%s\n' "${FAKE_TARGET_GID:-1000}"
    else
      printf '0\n'
    fi
    ;;
  -un)
    if [[ "$mode" == "target" ]]; then
      printf 'target\n'
    else
      printf 'root\n'
    fi
    ;;
  *)
    echo "unsupported id args: $*" >&2
    exit 1
    ;;
esac
""",
    )

    fake_chown = tmp_path / "chown"
    _write_executable(
        fake_chown,
        """#!/usr/bin/env bash
set -euo pipefail
echo "Operation not permitted" >&2
exit 1
""",
    )

    fake_sudo = tmp_path / "sudo"
    _write_executable(
        fake_sudo,
        """#!/usr/bin/env bash
set -euo pipefail
args=("$@")
idx=0
while [[ $idx -lt ${#args[@]} ]]; do
  case "${args[$idx]}" in
    -u|-g)
      idx=$((idx + 2))
      ;;
    env)
      idx=$((idx + 1))
      break
      ;;
    *)
      idx=$((idx + 1))
      ;;
  esac
done
while [[ $idx -lt ${#args[@]} && "${args[$idx]}" == *=* ]]; do
  export "${args[$idx]}"
  idx=$((idx + 1))
done
export FAKE_ID_MODE=target
exec "${args[@]:$idx}"
""",
    )

    env = os.environ.copy()
    env["K1S_ROOT_DIR_OVERRIDE"] = str(tmp_path)
    env["PATH"] = f"{tmp_path}:{env['PATH']}"
    env["FAKE_ID_MODE"] = "root"
    env["FAKE_TARGET_UID"] = str(os.getuid())
    env["FAKE_TARGET_GID"] = str(os.getgid())
    env["SUDO_UID"] = str(os.getuid())
    env["SUDO_GID"] = str(os.getgid())

    proc = subprocess.run(
        ["bash", str(PROFILE_STATE_OWNERSHIP_SCRIPT), "--profile", "k1s-core", "--repair"],
        check=False,
        text=True,
        capture_output=True,
        env=env,
    )

    assert proc.returncode == 0
    assert "warning: ownership normalization skipped for" in proc.stderr
    assert "Operation not permitted" in proc.stderr


def test_profile_state_ownership_repair_prefers_explicit_target_ids(tmp_path: Path) -> None:
    profile_dir = tmp_path / "state" / "profiles" / "k1s-core"
    edge_dir = profile_dir / "edge-ingress"
    edge_dir.mkdir(parents=True)
    (edge_dir / "envoy.yaml").write_text("static_resources: {}\n", encoding="utf-8")

    fake_id = tmp_path / "id"
    _write_executable(
        fake_id,
        """#!/usr/bin/env bash
set -euo pipefail
mode="${FAKE_ID_MODE:-root}"
case "${1:-}" in
  -u)
    if [[ "$mode" == "target" ]]; then
      printf '%s\n' "${FAKE_TARGET_UID:-1000}"
    else
      printf '0\n'
    fi
    ;;
  -g)
    if [[ "$mode" == "target" ]]; then
      printf '%s\n' "${FAKE_TARGET_GID:-1000}"
    else
      printf '0\n'
    fi
    ;;
  -un)
    if [[ "$mode" == "target" ]]; then
      printf 'target\n'
    else
      printf 'root\n'
    fi
    ;;
  *)
    echo "unsupported id args: $*" >&2
    exit 1
    ;;
esac
""",
    )

    fake_chown = tmp_path / "chown"
    _write_executable(
        fake_chown,
        """#!/usr/bin/env bash
set -euo pipefail
echo "Operation not permitted" >&2
exit 1
""",
    )

    fake_sudo = tmp_path / "sudo"
    _write_executable(
        fake_sudo,
        """#!/usr/bin/env bash
set -euo pipefail
args=("$@")
idx=0
requested_uid=""
requested_gid=""
while [[ $idx -lt ${#args[@]} ]]; do
  case "${args[$idx]}" in
    -u)
      requested_uid="${args[$((idx + 1))]}"
      idx=$((idx + 2))
      ;;
    -g)
      requested_gid="${args[$((idx + 1))]}"
      idx=$((idx + 2))
      ;;
    env)
      idx=$((idx + 1))
      break
      ;;
    *)
      idx=$((idx + 1))
      ;;
  esac
done
if [[ -n "${EXPECTED_SUDO_UID:-}" && "$requested_uid" != "#${EXPECTED_SUDO_UID}" ]]; then
  echo "unexpected sudo uid: $requested_uid" >&2
  exit 1
fi
if [[ -n "${EXPECTED_SUDO_GID:-}" && "$requested_gid" != "#${EXPECTED_SUDO_GID}" ]]; then
  echo "unexpected sudo gid: $requested_gid" >&2
  exit 1
fi
while [[ $idx -lt ${#args[@]} && "${args[$idx]}" == *=* ]]; do
  export "${args[$idx]}"
  idx=$((idx + 1))
done
export FAKE_ID_MODE=target
exec "${args[@]:$idx}"
""",
    )

    env = os.environ.copy()
    env["K1S_ROOT_DIR_OVERRIDE"] = str(tmp_path)
    env["PATH"] = f"{tmp_path}:{env['PATH']}"
    env["FAKE_ID_MODE"] = "root"
    env["FAKE_TARGET_UID"] = str(os.getuid())
    env["FAKE_TARGET_GID"] = str(os.getgid())
    env["EXPECTED_SUDO_UID"] = str(os.getuid())
    env["EXPECTED_SUDO_GID"] = str(os.getgid())
    env["SUDO_UID"] = "4242"
    env["SUDO_GID"] = "4343"

    proc = subprocess.run(
        [
            "bash",
            str(PROFILE_STATE_OWNERSHIP_SCRIPT),
            "--profile",
            "k1s-core",
            "--repair",
            "--target-uid",
            str(os.getuid()),
            "--target-gid",
            str(os.getgid()),
        ],
        check=False,
        text=True,
        capture_output=True,
        env=env,
    )

    assert proc.returncode == 0
    assert "warning: ownership normalization skipped for" in proc.stderr
    assert "unexpected sudo uid" not in proc.stderr


def test_profile_state_ownership_repair_skips_missing_guest_group_for_fallback(tmp_path: Path) -> None:
    profile_dir = tmp_path / "state" / "profiles" / "k1s-core"
    edge_dir = profile_dir / "edge-ingress"
    edge_dir.mkdir(parents=True)
    (edge_dir / "envoy.yaml").write_text("static_resources: {}\n", encoding="utf-8")
    missing_gid = str(os.getgid() + 1000)

    fake_id = tmp_path / "id"
    _write_executable(
        fake_id,
        """#!/usr/bin/env bash
set -euo pipefail
mode="${FAKE_ID_MODE:-root}"
case "${1:-}" in
  -u)
    if [[ "$mode" == "target" ]]; then
      printf '%s\n' "${FAKE_TARGET_UID:-1000}"
    else
      printf '0\n'
    fi
    ;;
  -g)
    if [[ "$mode" == "target" ]]; then
      printf '%s\n' "${FAKE_TARGET_GID:-1000}"
    else
      printf '0\n'
    fi
    ;;
  -un)
    if [[ "$mode" == "target" ]]; then
      printf 'target\n'
    else
      printf 'root\n'
    fi
    ;;
  *)
    echo "unsupported id args: $*" >&2
    exit 1
    ;;
esac
""",
    )

    fake_chown = tmp_path / "chown"
    _write_executable(
        fake_chown,
        """#!/usr/bin/env bash
set -euo pipefail
echo "Operation not permitted" >&2
exit 1
""",
    )

    fake_getent = tmp_path / "getent"
    _write_executable(
        fake_getent,
        """#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" == "group" && "${2:-}" == "${FAKE_EXISTING_GROUP_ID:-}" ]]; then
  printf 'target:x:%s:\n' "${2:-}"
  exit 0
fi
exit 2
""",
    )

    fake_sudo = tmp_path / "sudo"
    _write_executable(
        fake_sudo,
        """#!/usr/bin/env bash
set -euo pipefail
args=("$@")
idx=0
requested_uid=""
requested_gid=""
while [[ $idx -lt ${#args[@]} ]]; do
  case "${args[$idx]}" in
    -u)
      requested_uid="${args[$((idx + 1))]}"
      idx=$((idx + 2))
      ;;
    -g)
      requested_gid="${args[$((idx + 1))]}"
      idx=$((idx + 2))
      ;;
    env)
      idx=$((idx + 1))
      break
      ;;
    *)
      idx=$((idx + 1))
      ;;
  esac
done
if [[ -n "${EXPECTED_SUDO_UID:-}" && "$requested_uid" != "#${EXPECTED_SUDO_UID}" ]]; then
  echo "unexpected sudo uid: $requested_uid" >&2
  exit 1
fi
if [[ -n "$requested_gid" ]]; then
  echo "unexpected sudo gid: $requested_gid" >&2
  exit 1
fi
while [[ $idx -lt ${#args[@]} && "${args[$idx]}" == *=* ]]; do
  export "${args[$idx]}"
  idx=$((idx + 1))
done
export FAKE_ID_MODE=target
exec "${args[@]:$idx}"
""",
    )

    env = os.environ.copy()
    env["K1S_ROOT_DIR_OVERRIDE"] = str(tmp_path)
    env["PATH"] = f"{tmp_path}:{env['PATH']}"
    env["FAKE_ID_MODE"] = "root"
    env["FAKE_TARGET_UID"] = str(os.getuid())
    env["FAKE_TARGET_GID"] = str(os.getgid())
    env["FAKE_EXISTING_GROUP_ID"] = str(os.getgid())
    env["EXPECTED_SUDO_UID"] = str(os.getuid())
    env["SUDO_UID"] = "4242"
    env["SUDO_GID"] = "4343"

    proc = subprocess.run(
        [
            "bash",
            str(PROFILE_STATE_OWNERSHIP_SCRIPT),
            "--profile",
            "k1s-core",
            "--repair",
            "--target-uid",
            str(os.getuid()),
            "--target-gid",
            missing_gid,
        ],
        check=False,
        text=True,
        capture_output=True,
        env=env,
    )

    assert proc.returncode == 0
    assert "warning: ownership normalization skipped for" in proc.stderr
    assert "unexpected sudo gid" not in proc.stderr


def test_profile_state_ownership_repair_fails_when_non_chownable_state_is_not_usable(tmp_path: Path) -> None:
    profile_dir = tmp_path / "state" / "profiles" / "k1s-core"
    edge_dir = profile_dir / "edge-ingress"
    edge_dir.mkdir(parents=True)
    (edge_dir / "envoy.yaml").write_text("static_resources: {}\n", encoding="utf-8")
    (edge_dir / "envoy.yaml").chmod(0o444)

    fake_id = tmp_path / "id"
    _write_executable(
        fake_id,
        """#!/usr/bin/env bash
set -euo pipefail
mode="${FAKE_ID_MODE:-root}"
case "${1:-}" in
  -u)
    if [[ "$mode" == "target" ]]; then
      printf '%s\n' "${FAKE_TARGET_UID:-1000}"
    else
      printf '0\n'
    fi
    ;;
  -g)
    if [[ "$mode" == "target" ]]; then
      printf '%s\n' "${FAKE_TARGET_GID:-1000}"
    else
      printf '0\n'
    fi
    ;;
  -un)
    if [[ "$mode" == "target" ]]; then
      printf 'target\n'
    else
      printf 'root\n'
    fi
    ;;
  *)
    echo "unsupported id args: $*" >&2
    exit 1
    ;;
esac
""",
    )

    fake_chown = tmp_path / "chown"
    _write_executable(
        fake_chown,
        """#!/usr/bin/env bash
set -euo pipefail
echo "Operation not permitted" >&2
exit 1
""",
    )

    fake_sudo = tmp_path / "sudo"
    _write_executable(
        fake_sudo,
        """#!/usr/bin/env bash
set -euo pipefail
args=("$@")
idx=0
while [[ $idx -lt ${#args[@]} ]]; do
  case "${args[$idx]}" in
    -u|-g)
      idx=$((idx + 2))
      ;;
    env)
      idx=$((idx + 1))
      break
      ;;
    *)
      idx=$((idx + 1))
      ;;
  esac
done
while [[ $idx -lt ${#args[@]} && "${args[$idx]}" == *=* ]]; do
  export "${args[$idx]}"
  idx=$((idx + 1))
done
export FAKE_ID_MODE=target
exec "${args[@]:$idx}"
""",
    )

    env = os.environ.copy()
    env["K1S_ROOT_DIR_OVERRIDE"] = str(tmp_path)
    env["PATH"] = f"{tmp_path}:{env['PATH']}"
    env["FAKE_ID_MODE"] = "root"
    env["FAKE_TARGET_UID"] = str(os.getuid())
    env["FAKE_TARGET_GID"] = str(os.getgid())
    env["SUDO_UID"] = str(os.getuid())
    env["SUDO_GID"] = str(os.getgid())

    proc = subprocess.run(
        ["bash", str(PROFILE_STATE_OWNERSHIP_SCRIPT), "--profile", "k1s-core", "--repair"],
        check=False,
        text=True,
        capture_output=True,
        env=env,
    )

    assert proc.returncode == 1
    assert "managed strict-CRI profile state requires repair" in proc.stderr
    assert "failed to normalize strict-CRI profile state ownership" in proc.stderr


def test_profile_state_ownership_rejects_incomplete_explicit_target_ids(tmp_path: Path) -> None:
    env = os.environ.copy()
    env["K1S_ROOT_DIR_OVERRIDE"] = str(tmp_path)

    proc = subprocess.run(
        [
            "bash",
            str(PROFILE_STATE_OWNERSHIP_SCRIPT),
            "--profile",
            "k1s-core",
            "--check",
            "--target-uid",
            "1001",
        ],
        check=False,
        text=True,
        capture_output=True,
        env=env,
    )

    assert proc.returncode == 1
    assert "--target-uid and --target-gid must be provided together" in proc.stderr


def test_profile_state_ownership_rejects_non_numeric_explicit_target_ids(tmp_path: Path) -> None:
    env = os.environ.copy()
    env["K1S_ROOT_DIR_OVERRIDE"] = str(tmp_path)

    proc = subprocess.run(
        [
            "bash",
            str(PROFILE_STATE_OWNERSHIP_SCRIPT),
            "--profile",
            "k1s-core",
            "--check",
            "--target-uid",
            "ae",
            "--target-gid",
            "1001",
        ],
        check=False,
        text=True,
        capture_output=True,
        env=env,
    )

    assert proc.returncode == 1
    assert "--target-uid must be numeric" in proc.stderr


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


def test_cri_image_mirror_ctr_normalizes_to_platform_before_push(tmp_path: Path) -> None:
    hosts_dir = tmp_path / "hosts.d"
    hosts_dir.mkdir()
    ctr_log = tmp_path / "ctr.log"
    crictl_log = tmp_path / "crictl.log"

    fake_ctr = tmp_path / "ctr"
    _write_executable(
        fake_ctr,
        """#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >>"$FAKE_CTR_LOG"
if [[ "${1:-}" == "images" && "${2:-}" == "convert" && "${3:-}" == "--help" ]]; then
  exit 0
fi
args=("$@")
if [[ "${1:-}" == "-n" ]]; then
  args=("${@:3}")
fi
case "${args[0]:-} ${args[1]:-}" in
  "images ls")
    exit 0
    ;;
  "images pull")
    exit 0
    ;;
  "images convert")
    exit 0
    ;;
  "images push")
    exit 0
    ;;
  "images delete")
    exit 0
    ;;
  "images tag")
    echo "ctr tag should not be used in mirror flow" >&2
    exit 1
    ;;
  *)
    echo "unexpected ctr args: ${args[*]}" >&2
    exit 1
    ;;
esac
""",
    )

    fake_crictl = tmp_path / "crictl"
    _write_executable(
        fake_crictl,
        """#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >>"$FAKE_CRICTL_LOG"
exit 0
""",
    )

    env = os.environ.copy()
    env["PATH"] = f"{tmp_path}:{env['PATH']}"
    env["FAKE_CTR_LOG"] = str(ctr_log)
    env["FAKE_CRICTL_LOG"] = str(crictl_log)
    env["AE_CRI_IMAGE_MIRROR_BACKEND"] = "ctr"
    env["AE_CRI_LOCAL_BUILD_BACKEND"] = "podman"
    env["AE_CTR_NAMESPACE"] = "k8s.io"
    env["AE_CTR_PLATFORM"] = "linux/amd64"
    env["AE_CTR_HOSTS_DIR"] = str(hosts_dir)
    env["AE_CRI_ENDPOINT"] = "unix:///tmp/fake-containerd.sock"

    proc = subprocess.run(
        [
            "bash",
            str(CRI_IMAGE_MIRROR_SCRIPT),
            "--source",
            "quay.io/coreos/etcd:v3.5.13",
            "--target",
            "localhost:5001/coreos/etcd:v3.5.13",
        ],
        check=True,
        text=True,
        capture_output=True,
        env=env,
    )

    ctr_lines = ctr_log.read_text(encoding="utf-8").splitlines()
    crictl_lines = crictl_log.read_text(encoding="utf-8").splitlines()

    assert any(
        line
        == f"-n k8s.io images pull --platform linux/amd64 --hosts-dir {hosts_dir} quay.io/coreos/etcd:v3.5.13"
        for line in ctr_lines
    )
    assert ctr_lines.count("-n k8s.io images delete localhost:5001/coreos/etcd:v3.5.13") == 2
    assert any(
        line
        == "-n k8s.io images convert --platform linux/amd64 quay.io/coreos/etcd:v3.5.13 localhost:5001/coreos/etcd:v3.5.13"
        for line in ctr_lines
    )
    assert any(
        line
        == f"-n k8s.io images push --platform linux/amd64 --hosts-dir {hosts_dir} localhost:5001/coreos/etcd:v3.5.13"
        for line in ctr_lines
    )
    assert "evicting ctr image ref (pre-convert)" in proc.stdout
    assert "evicting CRI image cache (pre-pull)" in proc.stdout
    assert "evicting ctr image ref (pre-pull)" in proc.stdout
    assert "normalizing ctr source to linux/amd64" in proc.stdout
    assert crictl_lines == [
        "--runtime-endpoint unix:///tmp/fake-containerd.sock rmi localhost:5001/coreos/etcd:v3.5.13",
        "--runtime-endpoint unix:///tmp/fake-containerd.sock pull localhost:5001/coreos/etcd:v3.5.13"
    ]


def test_cri_image_mirror_ctr_continues_when_cri_evict_is_missing(tmp_path: Path) -> None:
    hosts_dir = tmp_path / "hosts.d"
    hosts_dir.mkdir()
    ctr_log = tmp_path / "ctr.log"
    crictl_log = tmp_path / "crictl.log"

    fake_ctr = tmp_path / "ctr"
    _write_executable(
        fake_ctr,
        """#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >>"$FAKE_CTR_LOG"
if [[ "${1:-}" == "images" && "${2:-}" == "convert" && "${3:-}" == "--help" ]]; then
  exit 0
fi
args=("$@")
if [[ "${1:-}" == "-n" ]]; then
  args=("${@:3}")
fi
case "${args[0]:-} ${args[1]:-}" in
  "images ls"|"images pull"|"images convert"|"images push"|"images delete")
    exit 0
    ;;
  *)
    echo "unexpected ctr args: ${args[*]}" >&2
    exit 1
    ;;
esac
""",
    )

    fake_crictl = tmp_path / "crictl"
    _write_executable(
        fake_crictl,
        """#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >>"$FAKE_CRICTL_LOG"
if [[ "${3:-}" == "rmi" ]]; then
  exit 1
fi
exit 0
""",
    )

    env = os.environ.copy()
    env["PATH"] = f"{tmp_path}:{env['PATH']}"
    env["FAKE_CTR_LOG"] = str(ctr_log)
    env["FAKE_CRICTL_LOG"] = str(crictl_log)
    env["AE_CRI_IMAGE_MIRROR_BACKEND"] = "ctr"
    env["AE_CTR_NAMESPACE"] = "k8s.io"
    env["AE_CTR_PLATFORM"] = "linux/amd64"
    env["AE_CTR_HOSTS_DIR"] = str(hosts_dir)
    env["AE_CRI_ENDPOINT"] = "unix:///tmp/fake-containerd.sock"

    proc = subprocess.run(
        [
            "bash",
            str(CRI_IMAGE_MIRROR_SCRIPT),
            "--source",
            "quay.io/coreos/etcd:v3.5.13",
            "--target",
            "localhost:5001/coreos/etcd:v3.5.13",
        ],
        check=True,
        text=True,
        capture_output=True,
        env=env,
    )

    assert "CRI pull verify localhost:5001/coreos/etcd:v3.5.13" in proc.stdout
    crictl_lines = crictl_log.read_text(encoding="utf-8").splitlines()
    assert crictl_lines == [
        "--runtime-endpoint unix:///tmp/fake-containerd.sock rmi localhost:5001/coreos/etcd:v3.5.13",
        "--runtime-endpoint unix:///tmp/fake-containerd.sock pull localhost:5001/coreos/etcd:v3.5.13",
    ]


def test_cri_image_mirror_ctr_requires_convert_support(tmp_path: Path) -> None:
    fake_ctr = tmp_path / "ctr"
    _write_executable(
        fake_ctr,
        """#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" == "images" && "${2:-}" == "convert" && "${3:-}" == "--help" ]]; then
  exit 1
fi
args=("$@")
if [[ "${1:-}" == "-n" ]]; then
  args=("${@:3}")
fi
case "${args[0]:-} ${args[1]:-}" in
  "images ls"|"images pull")
    exit 0
    ;;
  *)
    echo "unexpected ctr args: ${args[*]}" >&2
    exit 1
    ;;
esac
""",
    )

    env = os.environ.copy()
    env["PATH"] = f"{tmp_path}:{env['PATH']}"
    env["AE_CRI_IMAGE_MIRROR_BACKEND"] = "ctr"
    env["AE_CTR_PLATFORM"] = "linux/amd64"

    proc = subprocess.run(
        [
            "bash",
            str(CRI_IMAGE_MIRROR_SCRIPT),
            "--source",
            "quay.io/coreos/etcd:v3.5.13",
            "--target",
            "localhost:5001/coreos/etcd:v3.5.13",
            "--no-pull-cri",
        ],
        check=False,
        text=True,
        capture_output=True,
        env=env,
    )

    assert proc.returncode == 1
    assert "ctr backend requires 'ctr images convert' support" in proc.stderr
    assert "AE_CRI_IMAGE_MIRROR_BACKEND=podman|nerdctl|docker" in proc.stderr


def test_build_cri_apishim_image_prefers_build_backend_env(tmp_path: Path) -> None:
    build_log = tmp_path / "build.log"
    crictl_log = tmp_path / "crictl.log"

    fake_podman = tmp_path / "podman"
    _write_executable(
        fake_podman,
        """#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >>"$FAKE_BUILD_LOG"
exit 0
""",
    )
    fake_docker = tmp_path / "docker"
    _write_executable(
        fake_docker,
        """#!/usr/bin/env bash
set -euo pipefail
echo "docker should not be used" >&2
exit 1
""",
    )
    fake_crictl = tmp_path / "crictl"
    _write_executable(
        fake_crictl,
        """#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >>"$FAKE_CRICTL_LOG"
exit 0
""",
    )

    env = os.environ.copy()
    env["PATH"] = f"{tmp_path}:{env['PATH']}"
    env["FAKE_BUILD_LOG"] = str(build_log)
    env["FAKE_CRICTL_LOG"] = str(crictl_log)
    env["AE_CRI_IMAGE_BUILD_BACKEND"] = "podman"
    env["AE_CRI_LOCAL_BUILD_BACKEND"] = "docker"
    env["AE_CRI_ENDPOINT"] = "unix:///tmp/fake-containerd.sock"

    proc = subprocess.run(
        [
            "bash",
            str(BUILD_CRI_APISHIM_IMAGE_SCRIPT),
            "--image",
            "localhost:5001/k1s-apishim:dev",
        ],
        check=True,
        text=True,
        capture_output=True,
        env=env,
    )

    build_lines = build_log.read_text(encoding="utf-8").splitlines()
    crictl_lines = crictl_log.read_text(encoding="utf-8").splitlines()
    assert build_lines == [
        f"build --network host -f {ROOT / 'ops' / 'images' / 'apishim.Dockerfile'} -t localhost:5001/k1s-apishim:dev {ROOT}",
        "push localhost:5001/k1s-apishim:dev",
    ]
    assert crictl_lines == [
        "--runtime-endpoint unix:///tmp/fake-containerd.sock pull localhost:5001/k1s-apishim:dev"
    ]
    assert "[build-cri-apishim] backend=podman" in proc.stdout
    assert "[build-cri-apishim] using host networking for podman build" in proc.stdout


def test_build_cri_apishim_image_uses_host_network_for_docker(tmp_path: Path) -> None:
    build_log = tmp_path / "build.log"

    fake_docker = tmp_path / "docker"
    _write_executable(
        fake_docker,
        """#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >>"$FAKE_BUILD_LOG"
exit 0
""",
    )
    fake_podman = tmp_path / "podman"
    _write_executable(
        fake_podman,
        """#!/usr/bin/env bash
set -euo pipefail
echo "podman should not be used" >&2
exit 1
""",
    )

    env = os.environ.copy()
    env["PATH"] = f"{tmp_path}:{env['PATH']}"
    env["FAKE_BUILD_LOG"] = str(build_log)
    env["AE_CRI_IMAGE_BUILD_BACKEND"] = "docker"

    proc = subprocess.run(
        [
            "bash",
            str(BUILD_CRI_APISHIM_IMAGE_SCRIPT),
            "--image",
            "localhost:5001/k1s-apishim:dev",
            "--no-pull-cri",
        ],
        check=True,
        text=True,
        capture_output=True,
        env=env,
    )

    build_lines = build_log.read_text(encoding="utf-8").splitlines()
    assert build_lines == [
        f"build --network host -f {ROOT / 'ops' / 'images' / 'apishim.Dockerfile'} -t localhost:5001/k1s-apishim:dev {ROOT}",
        "push localhost:5001/k1s-apishim:dev",
    ]
    assert "[build-cri-apishim] backend=docker" in proc.stdout
    assert "[build-cri-apishim] using host networking for docker build" in proc.stdout


def test_build_cri_apishim_image_leaves_nerdctl_build_network_unchanged(tmp_path: Path) -> None:
    build_log = tmp_path / "build.log"

    fake_nerdctl = tmp_path / "nerdctl"
    _write_executable(
        fake_nerdctl,
        """#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >>"$FAKE_BUILD_LOG"
exit 0
""",
    )
    fake_buildctl = tmp_path / "buildctl"
    _write_executable(fake_buildctl, "#!/usr/bin/env bash\nexit 0\n")
    fake_podman = tmp_path / "podman"
    _write_executable(
        fake_podman,
        """#!/usr/bin/env bash
set -euo pipefail
echo "podman should not be used" >&2
exit 1
""",
    )
    fake_docker = tmp_path / "docker"
    _write_executable(
        fake_docker,
        """#!/usr/bin/env bash
set -euo pipefail
echo "docker should not be used" >&2
exit 1
""",
    )

    env = os.environ.copy()
    env["PATH"] = f"{tmp_path}:{env['PATH']}"
    env["FAKE_BUILD_LOG"] = str(build_log)
    env["AE_CRI_IMAGE_BUILD_BACKEND"] = "nerdctl"

    proc = subprocess.run(
        [
            "bash",
            str(BUILD_CRI_APISHIM_IMAGE_SCRIPT),
            "--image",
            "localhost:5001/k1s-apishim:dev",
            "--no-pull-cri",
        ],
        check=True,
        text=True,
        capture_output=True,
        env=env,
    )

    build_lines = build_log.read_text(encoding="utf-8").splitlines()
    assert build_lines == [
        f"build -f {ROOT / 'ops' / 'images' / 'apishim.Dockerfile'} -t localhost:5001/k1s-apishim:dev {ROOT}",
        "push localhost:5001/k1s-apishim:dev",
    ]
    assert "[build-cri-apishim] backend=nerdctl" in proc.stdout
    assert "using host networking" not in proc.stdout


def test_build_cri_apishim_image_rejects_ctr_backend(tmp_path: Path) -> None:
    env = os.environ.copy()
    env["AE_CRI_IMAGE_BUILD_BACKEND"] = "ctr"

    proc = subprocess.run(
        [
            "bash",
            str(BUILD_CRI_APISHIM_IMAGE_SCRIPT),
            "--image",
            "localhost:5001/k1s-apishim:dev",
            "--no-pull-cri",
        ],
        check=False,
        text=True,
        capture_output=True,
        env=env,
    )

    assert proc.returncode == 1
    assert "Requested build backend 'ctr' is invalid" in proc.stderr
    assert "AE_CRI_IMAGE_BUILD_BACKEND=podman|docker|nerdctl" in proc.stderr


def test_cri_preflight_reports_path_only_cni_plugins(tmp_path: Path) -> None:
    path_bin = tmp_path / "cni-plugins" / "bin"
    missing_cni_dir = tmp_path / "missing-cni"
    path_bin.mkdir(parents=True)

    for plugin in REQUIRED_CNI_PLUGINS:
        _write_fake_plugin(path_bin / plugin)

    env = os.environ.copy()
    env["K1S_OS_ID_OVERRIDE"] = "debian"
    env["AE_CRI_ENDPOINT"] = "tcp://127.0.0.1:12345"
    env["CRICTL_BIN"] = "definitely-not-installed"
    env["CNI_BIN_DIR"] = str(missing_cni_dir)
    env["PATH"] = f"{path_bin}:{env['PATH']}"

    proc = subprocess.run(
        ["bash", str(CRI_PREFLIGHT_SCRIPT)],
        check=False,
        text=True,
        capture_output=True,
        env=env,
    )

    assert proc.returncode == 1
    assert f"CNI bin dir missing: {missing_cni_dir}" in proc.stderr
    assert "CNI plugins detected on PATH but not at" in proc.stderr


def test_cri_preflight_reports_containerd_permission_hint(tmp_path: Path) -> None:
    sock_path = tmp_path / "containerd.sock"
    cni_bin = tmp_path / "cni-bin"
    cni_conf = tmp_path / "cni-conf"
    cni_bin.mkdir()
    cni_conf.mkdir()
    for plugin in REQUIRED_CNI_PLUGINS:
        _write_fake_plugin(cni_bin / plugin)

    fake_crictl = tmp_path / "crictl"
    fake_crictl.write_text(
        "#!/usr/bin/env bash\necho 'rpc error: permission denied' >&2\nexit 1\n",
        encoding="utf-8",
    )
    fake_crictl.chmod(0o755)

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(sock_path))
    try:
        env = os.environ.copy()
        env["AE_CRI_ENDPOINT"] = f"unix://{sock_path}"
        env["CRICTL_BIN"] = str(fake_crictl)
        env["CNI_BIN_DIR"] = str(cni_bin)
        env["CNI_CONF_DIR"] = str(cni_conf)

        proc = subprocess.run(
            ["bash", str(CRI_PREFLIGHT_SCRIPT)],
            check=False,
            text=True,
            capture_output=True,
            env=env,
        )
    finally:
        server.close()

    assert proc.returncode == 0
    assert "containerd_socket_access.sh --grant" in proc.stderr
    assert "CRI preflight OK" in proc.stdout


def test_cri_preflight_uses_live_containerd_cni_paths_for_nixos(tmp_path: Path) -> None:
    nixos_root = tmp_path / "etc" / "nixos"
    module_dest = nixos_root / "nixos" / "modules" / "k1s-cri-host.nix"
    module_dest.parent.mkdir(parents=True, exist_ok=True)
    module_dest.write_text("{ ... }: {}\n", encoding="utf-8")
    (nixos_root / "configuration.nix").write_text(
        "imports = [ ./nixos/modules/k1s-cri-host.nix ];\n",
        encoding="utf-8",
    )

    cni_bin = tmp_path / "nix-store-cni" / "bin"
    cni_conf = tmp_path / "cni-conf"
    cni_bin.mkdir(parents=True)
    cni_conf.mkdir()
    for plugin in REQUIRED_CNI_PLUGINS:
        _write_fake_plugin(cni_bin / plugin)
    (cni_conf / "10-k1s-bridge.conflist").write_text("{}", encoding="utf-8")

    fake_containerd = tmp_path / "containerd"
    fake_containerd.write_text(
        f"""#!/usr/bin/env bash
set -euo pipefail
if [[ "${{1:-}}" == "config" && "${{2:-}}" == "dump" ]]; then
  cat <<'EOF'
version = 3
[plugins.'io.containerd.cri.v1.runtime'.cni]
  bin_dirs = ['{cni_bin}']
  conf_dir = '{cni_conf}'
EOF
  exit 0
fi
exit 1
""",
        encoding="utf-8",
    )
    fake_containerd.chmod(0o755)

    env = os.environ.copy()
    env["K1S_OS_ID_OVERRIDE"] = "nixos"
    env["AE_CRI_ENDPOINT"] = "tcp://127.0.0.1:12345"
    env["CRICTL_BIN"] = "definitely-not-installed"
    env["CONTAINERD_BIN"] = str(fake_containerd)
    env["AE_NIXOS_SEARCH_ROOT"] = str(nixos_root)
    env["AE_NIXOS_CRI_MODULE_DEST"] = str(module_dest)

    proc = subprocess.run(
        ["bash", str(CRI_PREFLIGHT_SCRIPT)],
        check=False,
        text=True,
        capture_output=True,
        env=env,
    )

    assert proc.returncode == 0
    assert "CRI preflight OK" in proc.stdout


def test_cri_preflight_requires_nixos_cri_module(tmp_path: Path) -> None:
    nixos_root = tmp_path / "etc" / "nixos"
    nixos_root.mkdir(parents=True)
    module_dest = nixos_root / "nixos" / "modules" / "k1s-cri-host.nix"

    fake_containerd = tmp_path / "containerd"
    fake_containerd.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" == "config" && "${2:-}" == "dump" ]]; then
  cat <<'EOF'
version = 3
[plugins.'io.containerd.cri.v1.runtime'.cni]
  bin_dirs = ['/nix/store/unit-cni/bin']
  conf_dir = '/etc/cni/net.d'
EOF
  exit 0
fi
exit 1
""",
        encoding="utf-8",
    )
    fake_containerd.chmod(0o755)

    env = os.environ.copy()
    env["K1S_OS_ID_OVERRIDE"] = "nixos"
    env["AE_CRI_ENDPOINT"] = "tcp://127.0.0.1:12345"
    env["CRICTL_BIN"] = "definitely-not-installed"
    env["CONTAINERD_BIN"] = str(fake_containerd)
    env["AE_NIXOS_SEARCH_ROOT"] = str(nixos_root)
    env["AE_NIXOS_CRI_MODULE_DEST"] = str(module_dest)

    proc = subprocess.run(
        ["bash", str(CRI_PREFLIGHT_SCRIPT)],
        check=False,
        text=True,
        capture_output=True,
        env=env,
    )

    assert proc.returncode == 1
    assert "NixOS strict CRI requires the k1s CRI host module" in proc.stderr
    assert "ops/nixos/k1s-cri-host.nix" in proc.stderr


def test_cri_bootstrap_scripts_use_compatible_cni_default() -> None:
    cni_init_text = CNI_INIT_SCRIPT.read_text(encoding="utf-8")
    assert 'cni_version="${AE_CNI_VERSION:-0.4.0}"' in cni_init_text
    assert "compgen" not in cni_init_text
    assert "-print -quit | grep -q ." in cni_init_text
    bootstrap_text = CNI_BIN_BOOTSTRAP_SCRIPT.read_text(encoding="utf-8")
    assert "AE_CNI_REQUIRED_PLUGINS:-bridge,portmap,firewall,tuning,loopback" in bootstrap_text
    assert (
        "AE_CNI_BOOTSTRAP_SOURCE_DIRS:-/usr/lib/cni:/usr/local/lib/cni:/run/current-system/sw/bin"
        in bootstrap_text
    )
    assert "AE_CNI_BOOTSTRAP_MODE:-symlink" in bootstrap_text
    mirror_text = CRI_IMAGE_MIRROR_SCRIPT.read_text(encoding="utf-8")
    assert (
        'ctr -n "$ctr_namespace" images convert --platform "$ctr_platform" "$source" "$target_image"'
        in mirror_text
    )
    assert 'AE_CRI_IMAGE_MIRROR_BACKEND' in mirror_text
    assert 'crictl --runtime-endpoint "$cri_endpoint" rmi "$target_image"' in mirror_text
    assert 'ctr -n "$ctr_namespace" images delete "$target_image"' in mirror_text
    assert "evicting CRI image cache" in mirror_text
    assert "evicting ctr image ref" in mirror_text
    assert 'cmd+=("$image")' in mirror_text
    assert 'engine_push "$target_image"' in mirror_text
    assert "k1s-ctr-stage" not in mirror_text
    assert "ctr backend requires 'ctr images convert' support" in mirror_text
    build_text = BUILD_CRI_APISHIM_IMAGE_SCRIPT.read_text(encoding="utf-8")
    assert 'AE_CRI_IMAGE_BUILD_BACKEND' in build_text
    assert "Requested build backend 'ctr' is invalid" in build_text
    assert "AE_CRI_IMAGE_BUILD_BACKEND=podman|docker|nerdctl" in build_text
    assert 'cni_version="${AE_CNI_VERSION:-0.4.0}"' in CRI_CI_SETUP_SCRIPT.read_text(
        encoding="utf-8"
    )
    common_bootstrap = COMMON_BOOTSTRAP_SCRIPT.read_text(encoding="utf-8")
    assert common_bootstrap.count('"cniVersion": "0.4.0"') == 2
    assert 'sandbox_image="${AE_CRI_SANDBOX_IMAGE:-registry.k8s.io/pause:3.9}"' in common_bootstrap
    assert "write_containerd_bootstrap_config()" in common_bootstrap
    assert "ctr -n k8s.io images import \"$seed_bundle\"" in common_bootstrap
    assert 'AE_CRI_SANDBOX_IMAGE="$sandbox_image" AE_CRI_SMOKE_PULL=0 "$cri_smoke_script"' in common_bootstrap
    assert "guest_root_uuid()" in common_bootstrap
    assert "guest_root_label()" in common_bootstrap
    assert "guest_fstab_root_source()" in common_bootstrap
    assert "guest_grub_root_uuids()" in common_bootstrap
    assert "assert_guest_boot_contract()" in common_bootstrap
    assert "ensure_initramfs_module()" in common_bootstrap
    assert "write_virtio_root_modules()" in common_bootstrap
    assert "ensure_initramfs_module virtio_blk" in common_bootstrap
    assert "ensure_initramfs_module virtio_pci" in common_bootstrap
    assert "update-initramfs -u -k all" in common_bootstrap
    assert "update-grub" in common_bootstrap
    assert "bootstrap_contract_version" in common_bootstrap
    assert '"cni_version": "${expected_cni_version}"' in common_bootstrap
    gpu_bootstrap = GPU_BOOTSTRAP_SCRIPT.read_text(encoding="utf-8")
    assert "mkdir -p /etc/k1s-image" in gpu_bootstrap
    assert "cat >/etc/k1s-image/gpu-info.json <<JSON" in gpu_bootstrap

    image_build = IMAGE_BUILD_SCRIPT.read_text(encoding="utf-8")
    assert (
        'BOOTSTRAP_CONTRACT_VERSION="${BOOTSTRAP_CONTRACT_VERSION:-20260324-cni-0.4.0-smoke-v1}"'
        in image_build
    )
    assert 'EXPECTED_CNI_VERSION="${EXPECTED_CNI_VERSION:-0.4.0}"' in image_build
    assert 'SEED_BUNDLE_SCRIPT="${SEED_BUNDLE_SCRIPT:-$ROOT_DIR/scripts/lab/vm/image_seed_bundle.sh}"' in image_build
    assert (
        'ASSERT_IMAGE_BOOT_CONTRACT_SCRIPT="${ASSERT_IMAGE_BOOT_CONTRACT_SCRIPT:-$ROOT_DIR/scripts/lab/vm/assert_image_boot_contract.sh}"'
        in image_build
    )
    assert 'SEED_BUNDLE="$ROOT_DIR/state/lab-vm/$SEED_RUN_ID/seeds/cri-seed-images.oci.tar"' in image_build
    assert '-var "seed_bundle=${SEED_BUNDLE}" \\' in image_build
    assert 'bash "$ASSERT_IMAGE_BOOT_CONTRACT_SCRIPT" "$image"' in image_build
    assert "bootstrap_contract_version:$bootstrap_contract_version" in image_build
    assert "cni_version:$cni_version" in image_build

    image_verify = IMAGE_VERIFY_SCRIPT.read_text(encoding="utf-8")
    assert (
        'BOOTSTRAP_CONTRACT_VERSION="${BOOTSTRAP_CONTRACT_VERSION:-20260324-cni-0.4.0-smoke-v1}"'
        in image_verify
    )
    assert 'EXPECTED_CNI_VERSION="${EXPECTED_CNI_VERSION:-0.4.0}"' in image_verify
    assert "--metadata-only" in image_verify
    assert "--purge-failed" in image_verify
    assert "boot_verify_image() (" in image_verify
    assert 'INSPECT_QCOW_BOOT_SCRIPT="${INSPECT_QCOW_BOOT_SCRIPT:-$ROOT_DIR/scripts/lab/vm/inspect_qcow_boot.sh}"' in image_verify
    assert (
        'ASSERT_IMAGE_BOOT_CONTRACT_SCRIPT="${ASSERT_IMAGE_BOOT_CONTRACT_SCRIPT:-$ROOT_DIR/scripts/lab/vm/assert_image_boot_contract.sh}"'
        in image_verify
    )
    assert 'bash "$VARIANT_UP_SCRIPT" --variant "$variant_file" --run-id "$run_id"' in image_verify
    assert 'bash "$ASSERT_IMAGE_BOOT_CONTRACT_SCRIPT" "$image"' in image_verify
    assert 'bash "$INSPECT_QCOW_BOOT_SCRIPT" "$state_dir/image-verify-${image_variant}.qcow2"' in image_verify
    assert 'echo "[image-verify] ssh key path: $key_path"' in image_verify
    assert ".bootstrap_contract_version == $v" in image_verify
    assert ".cni_version == $v" in image_verify
    packer_template = PACKER_TEMPLATE.read_text(encoding="utf-8")
    assert 'disk_interface   = "virtio"' in packer_template


def test_guest_prereqs_runs_cri_sandbox_smoke() -> None:
    text = GUEST_PREREQS_SCRIPT.read_text(encoding="utf-8")
    assert "/mnt/host/scripts/cri_smoke.sh" in text
    assert "AE_CRI_SMOKE_PULL=0" in text
    assert "[vm-prereqs] cri-sandbox-smoke detail:" in text
    assert "tail -n 5" in text
    assert 'missing+=("cri-sandbox-smoke")' in text
