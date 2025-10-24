import os
from pathlib import Path
import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("AE_DOCKER_TEST"), reason="set AE_DOCKER_TEST=1 to run docker-backed tests"
)


def test_echo_projection_files_exist():
    proj = Path("state/projections")
    candidates = list(proj.glob("echo-rev*/"))
    assert candidates, "projection dir for echo not found; run init_demo.sh --demo-configs"
    root = candidates[0]
    assert (root / "config" / "mode").exists()
    assert (root / "secret" / "token").exists()

