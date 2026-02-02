"""Types for storage controller and NetFS plumbing."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class PvcRef:
    """Reference to a PersistentVolumeClaim."""

    name: str
    namespace: str
    uid: str | None = None

    def key(self) -> tuple[str, str]:
        return (self.namespace, self.name)


@dataclass(slots=True, frozen=True)
class PvRef:
    """Reference to a PersistentVolume."""

    name: str
    uid: str | None = None
    driver: str | None = None

    def key(self) -> str:
        return self.name


@dataclass(slots=True)
class NetFSMount:
    """Resolved NetFS mount metadata for a PVC on a node."""

    pvc: PvcRef
    pv: PvRef
    node_id: str
    host_path: str
    read_only: bool = False
