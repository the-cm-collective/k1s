"""Pod CIDR allocator for multi-node overlay networking.

The allocator is intentionally simple: it walks subnets of a configured pool
and assigns the first free block to a node. Allocations are persisted in the
existing `nodes.pod_cidr` column via the state store.
"""

from __future__ import annotations

import ipaddress
import os

from ae.controller.state import SQLiteStateStore


class PodCIDRAllocator:
    """Stateful allocator backed by the nodes table."""

    def __init__(self, store: SQLiteStateStore, pool: str, mask: int) -> None:
        self._store = store
        self._pool = ipaddress.ip_network(pool)
        self._mask = int(mask)
        if self._mask < self._pool.prefixlen or self._mask > self._pool.max_prefixlen:
            raise ValueError(f"mask /{mask} invalid for pool {pool}")

    @classmethod
    def from_env(cls, store: SQLiteStateStore) -> PodCIDRAllocator:
        pool = os.getenv("AE_POD_CIDR_POOL", "10.42.0.0/16")
        mask = int(os.getenv("AE_POD_CIDR_MASK", "24") or 24)
        return cls(store, pool, mask)

    def _used(self) -> set[ipaddress.IPv4Network | ipaddress.IPv6Network]:
        used: set[ipaddress._BaseNetwork] = set()
        for node, _ in self._store.list_nodes():
            cidr = getattr(node, "pod_cidr", None)
            if not cidr:
                continue
            try:
                used.add(ipaddress.ip_network(cidr, strict=False))
            except Exception:
                continue
        return used

    def allocate(self, node_id: str) -> str:
        """Return an assigned CIDR for the node (existing or newly allocated)."""
        # If already assigned, return it
        current = self._store.get_node(node_id)
        if current:
            node, _ = current
            if node.pod_cidr:
                return str(node.pod_cidr)

        used = self._used()
        for subnet in self._pool.subnets(new_prefix=self._mask):
            if subnet in used:
                continue
            cidr = str(subnet)
            # Persist on the node record
            self._store.upsert_node(
                node_id,
                pod_cidr=cidr,
                name=getattr(node, "name", None) if current else None,
                labels=getattr(node, "labels", None) if current else None,
                taints=getattr(node, "taints", None) if current else None,
                backend=getattr(node, "backend", None) if current else None,
                endpoint=getattr(node, "endpoint", None) if current else None,
                wg_pubkey=getattr(node, "wg_pubkey", None) if current else None,
            )
            return cidr
        raise RuntimeError(f"no free pod CIDRs available in pool {self._pool} (/ {self._mask})")


# ruff: noqa: E501,UP006,UP007,UP017,UP035,S110,S112,SIM105
