import ipaddress

import pytest

from ae.controller.state import SQLiteStateStore
from ae.network.pod_cidr import PodCIDRAllocator


def test_allocator_assigns_unique_blocks(tmp_path):
    store = SQLiteStateStore(tmp_path / "state.db")
    alloc = PodCIDRAllocator(store, "10.50.0.0/29", 30)  # four subnets of /30

    a = alloc.allocate("n1")
    b = alloc.allocate("n2")

    assert a != b
    assert ipaddress.ip_network(a).prefixlen == 30
    assert ipaddress.ip_network(b).prefixlen == 30

    # Re-allocation returns existing block
    again = alloc.allocate("n1")
    assert again == a


def test_allocator_exhaustion_raises(tmp_path):
    store = SQLiteStateStore(tmp_path / "state.db")
    alloc = PodCIDRAllocator(store, "10.60.0.0/30", 30)  # only one /30

    first = alloc.allocate("n1")
    assert first
    with pytest.raises(RuntimeError):
        alloc.allocate("n2")
