from pathlib import Path

import pytest

from ae.controller.state import SQLiteStateStore


def test_site_ingress_port_allocation(tmp_path: Path) -> None:
    db_path = tmp_path / "controller.db"
    store = SQLiteStateStore(db_path=db_path)

    port_a = store.ensure_site_ingress_port("site-a", port_min=18080, port_max=18081)
    assert port_a in {18080, 18081}

    same = store.ensure_site_ingress_port("site-a", port_min=18080, port_max=18081)
    assert same == port_a

    port_b = store.ensure_site_ingress_port("site-b", port_min=18080, port_max=18081)
    assert port_b in {18080, 18081}
    assert port_b != port_a


def test_site_ingress_port_exhaustion(tmp_path: Path) -> None:
    db_path = tmp_path / "controller.db"
    store = SQLiteStateStore(db_path=db_path)

    store.ensure_site_ingress_port("site-a", port_min=18080, port_max=18080)
    with pytest.raises(RuntimeError):
        store.ensure_site_ingress_port("site-b", port_min=18080, port_max=18080)
