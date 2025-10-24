import os
import pytest


pytestmark = pytest.mark.skipif(
    not os.environ.get("AE_DOCKER_TEST"), reason="set AE_DOCKER_TEST=1 to run docker-backed tests"
)


def test_storage_volume_lifecycle():
    import subprocess
    # Apply storage demo manifest
    subprocess.run(["python", "-m", "ae.cli", "apply", "-f", "specs/examples/echo-storage.yaml"], check=True)
    # List volumes via CLI JSON
    import json
    out = subprocess.check_output(["python", "-m", "ae.cli", "volumes", "list", "--app", "echo", "--json"], text=True)
    vols = json.loads(out)
    names = [v.get("name", "") for v in vols]
    assert any(n.startswith("ae-echo-") for n in names)

    # Delete without purge — volume should remain (retention: Retain)
    subprocess.run(["python", "-m", "ae.cli", "delete", "echo"], check=True)
    out2 = subprocess.check_output(["python", "-m", "ae.cli", "volumes", "list", "--app", "echo", "--json"], text=True)
    vols2 = json.loads(out2)
    assert vols2, "volumes should remain with retention=Retain"