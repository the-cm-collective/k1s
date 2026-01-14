import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("AE_INTEG_RUNTIME"),
    reason="set AE_INTEG_RUNTIME=podman or docker to enable runtime-backed tests",
)


def test_echo_projection_files_exist():
    proj = Path("state/projections")
    candidates = list(proj.glob("echo-rev*/"))
    assert candidates, "projection dir for echo not found; run init_demo.sh --demo-configs"
    root = candidates[0]
    assert (root / "config" / "mode").exists()
    assert (root / "secret" / "token").exists()
