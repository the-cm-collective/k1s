import json
import os
import subprocess

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("AE_INTEG_RUNTIME"),
    reason="set AE_INTEG_RUNTIME=podman or docker to enable runtime-backed tests",
)


def test_storage_purge_removes_delete_retention():
    # Apply storage demo with retention Delete
    subprocess.run(
        ["python", "-m", "ae.cli", "apply", "-f", "specs/examples/echo-storage-delete.yaml"],
        check=True,
    )
    out = subprocess.check_output(
        ["python", "-m", "ae.cli", "volumes", "list", "--app", "echo-del", "--json"], text=True
    )
    vols = json.loads(out)
    assert vols, "expected volumes for echo-del"
    # Purge delete
    subprocess.run(["python", "-m", "ae.cli", "delete", "echo-del", "--purge"], check=True)
    out2 = subprocess.check_output(
        ["python", "-m", "ae.cli", "volumes", "list", "--app", "echo-del", "--json"], text=True
    )
    vols2 = json.loads(out2)
    assert not vols2, f"expected volumes to be removed on purge, got: {vols2}"


# ruff: noqa: S603,S607
