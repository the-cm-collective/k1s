from __future__ import annotations

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
VALIDATOR_SCRIPT = ROOT / "scripts" / "dev" / "netfs_validate.sh"
APISHIM_KUBECTL_SCRIPT = ROOT / "scripts" / "dev" / "apishim_kubectl.sh"
HOST_A_SMOKE_SCRIPT = ROOT / "scripts" / "dev" / "host_a_netfs_smoke.sh"


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def test_netfs_validate_help_mentions_remote_cri_flags() -> None:
    proc = subprocess.run(
        ["bash", str(VALIDATOR_SCRIPT), "--help"],
        check=True,
        text=True,
        capture_output=True,
        cwd=ROOT,
    )

    assert "--cri-host <host>" in proc.stdout
    assert "--cri-user <user>" in proc.stdout
    assert "--cri-key <path>" in proc.stdout
    assert "--cri-port <port>" in proc.stdout


def test_netfs_validate_remote_cri_fallback_uses_ssh(tmp_path: Path) -> None:
    ssh_log = tmp_path / "ssh.log"
    stamp_file = tmp_path / "stamp.txt"
    ae_log = tmp_path / "ae.log"
    fake_key = tmp_path / "id_rsa"
    fake_key.write_text("key", encoding="utf-8")

    fake_ae = tmp_path / "ae"
    _write_executable(
        fake_ae,
        """#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >>"$FAKE_AE_LOG"
echo "spdy upgrade failed: 404" >&2
exit 1
""",
    )

    fake_ssh = tmp_path / "ssh"
    _write_executable(
        fake_ssh,
        """#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >>"$FAKE_SSH_LOG"
remote_cmd="${@: -1}"
eval "set -- $remote_cmd"
remote_args=("$@")
cmd=()
seen_sep=0
for arg in "${remote_args[@]}"; do
  if [[ "$seen_sep" == "1" ]]; then
    cmd+=("$arg")
    continue
  fi
  if [[ "$arg" == "--" ]]; then
    seen_sep=1
  fi
done

subcmd="${cmd[0]:-}"
case "$subcmd" in
  info)
    echo "fake-cri-info"
    ;;
  ps)
    if [[ " ${cmd[*]} " == *" --pod pod-writer "* ]]; then
      echo "writer-cid"
    elif [[ " ${cmd[*]} " == *" --pod pod-reader "* ]]; then
      echo "reader-cid"
    fi
    ;;
  pods)
    if [[ " ${cmd[*]} " == *" --name writer "* ]] || [[ " ${cmd[*]} " == *" --name writer-app "* ]]; then
      echo "pod-writer"
    elif [[ " ${cmd[*]} " == *" --name reader "* ]] || [[ " ${cmd[*]} " == *" --name reader-app "* ]]; then
      echo "pod-reader"
    fi
    ;;
  exec)
    cid="${cmd[1]:-}"
    if [[ "$cid" == "writer-cid" ]]; then
      shell_cmd="${cmd[4]:-}"
      stamp="$(printf '%s' "$shell_cmd" | sed -n 's/^echo \\([^ ]*\\) > .*$/\\1/p')"
      printf '%s' "$stamp" >"$FAKE_STAMP_FILE"
      printf '%s\\n' "$stamp"
    else
      cat "$FAKE_STAMP_FILE"
      printf '\\n'
    fi
    ;;
  *)
    echo "unexpected ssh subcommand: $subcmd" >&2
    exit 1
    ;;
esac
""",
    )

    env = os.environ.copy()
    env["PATH"] = f"{tmp_path}:{env['PATH']}"
    env["FAKE_SSH_LOG"] = str(ssh_log)
    env["FAKE_STAMP_FILE"] = str(stamp_file)
    env["FAKE_AE_LOG"] = str(ae_log)

    proc = subprocess.run(
        [
            "bash",
            str(VALIDATOR_SCRIPT),
            "--writer-app",
            "writer-app",
            "--reader-app",
            "reader-app",
            "--runtime",
            "cri",
            "--cri-host",
            "192.0.2.10",
            "--cri-user",
            "ae",
            "--cri-key",
            str(fake_key),
        ],
        check=True,
        text=True,
        capture_output=True,
        cwd=ROOT,
        env=env,
    )

    out = proc.stdout
    ssh_calls = ssh_log.read_text(encoding="utf-8")
    ae_calls = ae_log.read_text(encoding="utf-8")

    assert "ae exec path not clean" in out
    assert "PASS: data path validated via cri runtime" in out
    assert "192.0.2.10" in ssh_calls
    assert "LogLevel=ERROR" in ssh_calls
    assert "exec writer-cid" in ssh_calls
    assert "exec reader-cid" in ssh_calls
    assert "exec -n default writer-app" in ae_calls
    assert "exec -n default reader-app" in ae_calls


def test_netfs_validate_remote_cri_exec_retries_transient_failure(tmp_path: Path) -> None:
    ssh_log = tmp_path / "ssh.log"
    stamp_file = tmp_path / "stamp.txt"
    writer_attempts = tmp_path / "writer-attempts.txt"
    ae_log = tmp_path / "ae.log"
    fake_key = tmp_path / "id_rsa"
    fake_key.write_text("key", encoding="utf-8")

    fake_ae = tmp_path / "ae"
    _write_executable(
        fake_ae,
        """#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >>"$FAKE_AE_LOG"
echo "spdy upgrade failed: 404" >&2
exit 1
""",
    )

    fake_ssh = tmp_path / "ssh"
    _write_executable(
        fake_ssh,
        """#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >>"$FAKE_SSH_LOG"
remote_cmd="${@: -1}"
eval "set -- $remote_cmd"
remote_args=("$@")
cmd=()
seen_sep=0
for arg in "${remote_args[@]}"; do
  if [[ "$seen_sep" == "1" ]]; then
    cmd+=("$arg")
    continue
  fi
  if [[ "$arg" == "--" ]]; then
    seen_sep=1
  fi
done

subcmd="${cmd[0]:-}"
case "$subcmd" in
  info)
    echo "fake-cri-info"
    ;;
  ps)
    if [[ " ${cmd[*]} " == *" --pod pod-writer "* ]]; then
      echo "writer-cid"
    elif [[ " ${cmd[*]} " == *" --pod pod-reader "* ]]; then
      echo "reader-cid"
    fi
    ;;
  pods)
    if [[ " ${cmd[*]} " == *" --name writer "* ]] || [[ " ${cmd[*]} " == *" --name writer-app "* ]]; then
      echo "pod-writer"
    elif [[ " ${cmd[*]} " == *" --name reader "* ]] || [[ " ${cmd[*]} " == *" --name reader-app "* ]]; then
      echo "pod-reader"
    fi
    ;;
  exec)
    cid="${cmd[1]:-}"
    if [[ "$cid" == "writer-cid" ]]; then
      attempts=0
      if [[ -f "$FAKE_WRITER_ATTEMPTS" ]]; then
        attempts="$(cat "$FAKE_WRITER_ATTEMPTS")"
      fi
      attempts=$((attempts + 1))
      printf '%s' "$attempts" >"$FAKE_WRITER_ATTEMPTS"
      if [[ "$attempts" == "1" ]]; then
        echo "bash: line 1: /data/hello.txt: No such file or directory" >&2
        exit 1
      fi
      shell_cmd="${cmd[4]:-}"
      stamp="$(printf '%s' "$shell_cmd" | sed -n 's/^echo \\([^ ]*\\) > .*$/\\1/p')"
      printf '%s' "$stamp" >"$FAKE_STAMP_FILE"
      printf '%s\\n' "$stamp"
    else
      cat "$FAKE_STAMP_FILE"
      printf '\\n'
    fi
    ;;
  *)
    echo "unexpected ssh subcommand: $subcmd" >&2
    exit 1
    ;;
esac
""",
    )

    env = os.environ.copy()
    env["PATH"] = f"{tmp_path}:{env['PATH']}"
    env["FAKE_SSH_LOG"] = str(ssh_log)
    env["FAKE_STAMP_FILE"] = str(stamp_file)
    env["FAKE_AE_LOG"] = str(ae_log)
    env["FAKE_WRITER_ATTEMPTS"] = str(writer_attempts)
    env["CRI_EXEC_RETRIES"] = "2"
    env["CRI_EXEC_RETRY_DELAY"] = "0"

    proc = subprocess.run(
        [
            "bash",
            str(VALIDATOR_SCRIPT),
            "--writer-app",
            "writer-app",
            "--reader-app",
            "reader-app",
            "--runtime",
            "cri",
            "--cri-host",
            "192.0.2.10",
            "--cri-user",
            "ae",
            "--cri-key",
            str(fake_key),
        ],
        check=True,
        text=True,
        capture_output=True,
        cwd=ROOT,
        env=env,
    )

    assert "PASS: data path validated via cri runtime" in proc.stdout
    assert "CRI exec attempt 1/2 failed for container writer-cid" in proc.stderr
    assert writer_attempts.read_text(encoding="utf-8") == "2"


def test_netfs_validate_retries_reader_before_fallback(tmp_path: Path) -> None:
    ae_log = tmp_path / "ae.log"
    stamp_file = tmp_path / "stamp.txt"
    reader_attempts = tmp_path / "reader-attempts.txt"

    fake_ae = tmp_path / "ae"
    _write_executable(
        fake_ae,
        """#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >>"$FAKE_AE_LOG"
if [[ "$1" != "exec" ]]; then
  echo "unexpected ae subcommand: $1" >&2
  exit 1
fi
app="${4:-}"
if [[ "$app" == "writer-app" ]]; then
  shell_cmd="$*"
  stamp="$(printf '%s' "$shell_cmd" | sed -n 's/.*echo \\([^ ]*\\) > .*/\\1/p')"
  printf '%s' "$stamp" >"$FAKE_STAMP_FILE"
  printf '%s\\n' "$stamp"
  exit 0
fi
if [[ "$app" == "reader-app" ]]; then
  attempts=0
  if [[ -f "$FAKE_READER_ATTEMPTS" ]]; then
    attempts="$(cat "$FAKE_READER_ATTEMPTS")"
  fi
  attempts=$((attempts + 1))
  printf '%s' "$attempts" >"$FAKE_READER_ATTEMPTS"
  if [[ "$attempts" == "1" ]]; then
    exit 0
  fi
  cat "$FAKE_STAMP_FILE"
  printf '\\n'
  exit 0
fi
echo "unexpected app: $app" >&2
exit 1
""",
    )

    env = os.environ.copy()
    env["PATH"] = f"{tmp_path}:{env['PATH']}"
    env["FAKE_AE_LOG"] = str(ae_log)
    env["FAKE_STAMP_FILE"] = str(stamp_file)
    env["FAKE_READER_ATTEMPTS"] = str(reader_attempts)
    env["AE_EXEC_READ_RETRIES"] = "2"
    env["AE_EXEC_READ_RETRY_DELAY"] = "0"

    proc = subprocess.run(
        [
            "bash",
            str(VALIDATOR_SCRIPT),
            "--writer-app",
            "writer-app",
            "--reader-app",
            "reader-app",
            "--runtime",
            "cri",
        ],
        check=True,
        text=True,
        capture_output=True,
        cwd=ROOT,
        env=env,
    )

    out = proc.stdout
    assert "reader miss on attempt 1/2" in out
    assert "PASS: netfs shared read/write via ae exec" in out
    assert "PASS: stream path clean" in out
    assert "ae exec path not clean" not in out
    assert reader_attempts.read_text(encoding="utf-8") == "2"


def test_apishim_kubectl_uses_read_only_token(tmp_path: Path) -> None:
    ca_bundle = tmp_path / "ca.pem"
    ca_bundle.write_text("dummy-ca", encoding="utf-8")
    kubectl_log = tmp_path / "kubectl.log"

    fake_ae = tmp_path / "ae"
    _write_executable(
        fake_ae,
        f"""#!/usr/bin/env bash
set -euo pipefail
cat <<'EOF'
export AE_APISHIM_SERVER=https://127.0.0.1:8445
export AE_APISHIM_TOKEN=admin-token
export AE_APISHIM_READ_TOKEN=read-token
export AE_APISHIM_CA_BUNDLE={ca_bundle}
EOF
""",
    )

    fake_kubectl = tmp_path / "kubectl"
    _write_executable(
        fake_kubectl,
        """#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >"$FAKE_KUBECTL_LOG"
""",
    )

    env = os.environ.copy()
    env["FAKE_KUBECTL_LOG"] = str(kubectl_log)

    subprocess.run(
        [
            "bash",
            str(APISHIM_KUBECTL_SCRIPT),
            "--ae-bin",
            str(fake_ae),
            "--kubectl-bin",
            str(fake_kubectl),
            "--read-only",
            "get",
            "pvc",
            "demo",
        ],
        check=True,
        text=True,
        capture_output=True,
        cwd=ROOT,
        env=env,
    )

    kubectl_args = kubectl_log.read_text(encoding="utf-8")

    assert "--server https://127.0.0.1:8445" in kubectl_args
    assert "--token read-token" in kubectl_args
    assert f"--certificate-authority {ca_bundle}" in kubectl_args
    assert "get pvc demo" in kubectl_args


def test_host_a_netfs_smoke_script_routes_pvc_through_apishim_helper() -> None:
    text = HOST_A_SMOKE_SCRIPT.read_text(encoding="utf-8")

    assert "apishim_kubectl.sh" in text
    assert 'apply -f "$pvc_manifest" --validate=false' in text
    assert '"$AE_BIN" apply -f "$writer_manifest"' in text
    assert '"$AE_BIN" apply -f "$reader_manifest"' in text
    assert '--cri-host "$GUEST_IP"' in text
