import os

import pytest

from tests.e2e.ha_closeout import run_ha_closeout_e2e


@pytest.mark.integration
def test_ha_closeout_e2e() -> None:
    if os.getenv("AE_E2E_HA_CLOSEOUT", "0") != "1":
        pytest.skip("set AE_E2E_HA_CLOSEOUT=1 to run the HA closeout e2e")
    run_ha_closeout_e2e()
