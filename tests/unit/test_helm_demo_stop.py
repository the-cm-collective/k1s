from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

from ae.observability.http_api import _HELM_DEMO_STATE, _helm_demo_stop


def _wait_for_file(path: Path, timeout: float = 2.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.exists() and path.read_text(encoding="utf-8").strip():
            return
        time.sleep(0.05)
    raise AssertionError(f"timeout waiting for {path}")


def test_helm_demo_stop_kills_process_group(tmp_path: Path) -> None:  # noqa: D401
    pid_file = tmp_path / "child.pid"
    script = tmp_path / "run.sh"
    script.write_text(
        "\n".join(
            [
                "#!/usr/bin/env bash",
                "set -euo pipefail",
                "sleep 60 &",
                "echo $! > child.pid",
                "wait",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    script.chmod(0o755)

    proc = subprocess.Popen(
        ["bash", str(script)],
        cwd=tmp_path,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    _wait_for_file(pid_file)
    child_pid = int(pid_file.read_text(encoding="utf-8").strip())

    _HELM_DEMO_STATE["proc"] = proc
    _helm_demo_stop()

    assert proc.poll() is not None
    # Child should be gone as well.
    try:
        os.kill(child_pid, 0)
    except OSError:
        return
    raise AssertionError("child process still alive after helm demo stop")
