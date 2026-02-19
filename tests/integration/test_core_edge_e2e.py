import os

import pytest

from tests.e2e.core_edge import run_core_edge_e2e


@pytest.mark.integration
def test_core_edge_e2e() -> None:
    if os.getenv("AE_E2E_CORE_EDGE", "0") != "1":
        pytest.skip("set AE_E2E_CORE_EDGE=1 to run core/edge e2e")
    run_core_edge_e2e()
