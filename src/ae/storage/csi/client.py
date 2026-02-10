"""CSI gRPC client helpers for controller and node operations."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

try:  # pragma: no cover - optional dependency
    import grpc
except Exception:  # pragma: no cover
    grpc = None

from .api import csi_pb2

try:  # pragma: no cover - optional dependency
    from .api import csi_pb2_grpc
except Exception:  # pragma: no cover
    csi_pb2_grpc = None


@dataclass(slots=True)
class CsiVolume:
    volume_id: str
    capacity_bytes: int | None
    volume_context: dict[str, str]


def normalize_endpoint(endpoint: str) -> str:
    if not endpoint:
        return endpoint
    raw = endpoint.strip()
    if raw.startswith("unix://"):
        # Ensure triple slash for absolute paths.
        if raw.startswith("unix:///"):
            return raw
        return "unix:///" + raw.removeprefix("unix://").lstrip("/")
    if raw.startswith("unix:"):
        return "unix:///" + raw.removeprefix("unix:").lstrip("/")
    if raw.startswith("/"):
        return "unix:///" + raw.lstrip("/")
    return raw


def build_channel(endpoint: str) -> grpc.Channel:
    if grpc is None:
        raise RuntimeError("grpc is required for CSI operations")
    return grpc.insecure_channel(normalize_endpoint(endpoint))


def _access_mode_from_modes(modes: Iterable[str]) -> int:
    mode_set = {str(m) for m in modes}
    if "ReadWriteMany" in mode_set:
        return csi_pb2.VolumeCapability.AccessMode.MULTI_NODE_MULTI_WRITER
    if "ReadOnlyMany" in mode_set:
        return csi_pb2.VolumeCapability.AccessMode.MULTI_NODE_READER_ONLY
    if "ReadWriteOncePod" in mode_set:
        return csi_pb2.VolumeCapability.AccessMode.SINGLE_NODE_WRITER
    if "ReadWriteOnce" in mode_set:
        return csi_pb2.VolumeCapability.AccessMode.SINGLE_NODE_WRITER
    return csi_pb2.VolumeCapability.AccessMode.UNKNOWN


def build_volume_capability(
    *,
    access_modes: Iterable[str],
    volume_mode: str,
    fs_type: str | None = None,
    mount_flags: Iterable[str] | None = None,
) -> csi_pb2.VolumeCapability:
    access_mode = csi_pb2.VolumeCapability.AccessMode(mode=_access_mode_from_modes(access_modes))
    if str(volume_mode).lower() == "block":
        block = csi_pb2.VolumeCapability.BlockVolume()
        return csi_pb2.VolumeCapability(access_mode=access_mode, block=block)
    mount = csi_pb2.VolumeCapability.MountVolume(
        fs_type=str(fs_type or ""),
        mount_flags=list(mount_flags or []),
    )
    return csi_pb2.VolumeCapability(access_mode=access_mode, mount=mount)


def _coalesce_context(raw: Mapping[str, Any] | None) -> dict[str, str]:
    if not isinstance(raw, Mapping):
        return {}
    out: dict[str, str] = {}
    for key, value in raw.items():
        if value is None:
            continue
        out[str(key)] = str(value)
    return out


class CsiControllerClient:
    def __init__(self, endpoint: str, *, timeout: float = 10.0) -> None:
        if grpc is None or csi_pb2_grpc is None:
            raise RuntimeError("grpc is required for CSI operations")
        self._endpoint = endpoint
        self._timeout = timeout
        self._channel = build_channel(endpoint)
        self._stub = csi_pb2_grpc.ControllerStub(self._channel)

    @property
    def endpoint(self) -> str:
        return self._endpoint

    def create_volume(
        self,
        *,
        name: str,
        capacity_bytes: int | None,
        volume_capabilities: list[csi_pb2.VolumeCapability],
        parameters: Mapping[str, Any] | None = None,
        secrets: Mapping[str, Any] | None = None,
        accessibility: csi_pb2.TopologyRequirement | None = None,
        volume_content_source: csi_pb2.VolumeContentSource | None = None,
    ) -> csi_pb2.CreateVolumeResponse:
        req = csi_pb2.CreateVolumeRequest(
            name=str(name),
            volume_capabilities=volume_capabilities,
            parameters=_coalesce_context(parameters),
            secrets=_coalesce_context(secrets),
            accessibility_requirements=accessibility,
            volume_content_source=volume_content_source,
        )
        if capacity_bytes is not None:
            req.capacity_range.required_bytes = int(capacity_bytes)
        return self._stub.CreateVolume(req, timeout=self._timeout)

    def delete_volume(
        self, *, volume_id: str, secrets: Mapping[str, Any] | None = None
    ) -> csi_pb2.DeleteVolumeResponse:
        req = csi_pb2.DeleteVolumeRequest(
            volume_id=str(volume_id),
            secrets=_coalesce_context(secrets),
        )
        return self._stub.DeleteVolume(req, timeout=self._timeout)

    def controller_publish(
        self,
        *,
        volume_id: str,
        node_id: str,
        volume_capability: csi_pb2.VolumeCapability,
        read_only: bool,
        secrets: Mapping[str, Any] | None = None,
        volume_context: Mapping[str, Any] | None = None,
    ) -> csi_pb2.ControllerPublishVolumeResponse:
        req = csi_pb2.ControllerPublishVolumeRequest(
            volume_id=str(volume_id),
            node_id=str(node_id),
            volume_capability=volume_capability,
            readonly=bool(read_only),
            secrets=_coalesce_context(secrets),
            volume_context=_coalesce_context(volume_context),
        )
        return self._stub.ControllerPublishVolume(req, timeout=self._timeout)

    def controller_unpublish(
        self,
        *,
        volume_id: str,
        node_id: str | None,
        secrets: Mapping[str, Any] | None = None,
    ) -> csi_pb2.ControllerUnpublishVolumeResponse:
        req = csi_pb2.ControllerUnpublishVolumeRequest(
            volume_id=str(volume_id),
            node_id=str(node_id) if node_id else "",
            secrets=_coalesce_context(secrets),
        )
        return self._stub.ControllerUnpublishVolume(req, timeout=self._timeout)


class CsiNodeClient:
    def __init__(self, endpoint: str, *, timeout: float = 10.0) -> None:
        if grpc is None or csi_pb2_grpc is None:
            raise RuntimeError("grpc is required for CSI operations")
        self._endpoint = endpoint
        self._timeout = timeout
        self._channel = build_channel(endpoint)
        self._stub = csi_pb2_grpc.NodeStub(self._channel)

    @property
    def endpoint(self) -> str:
        return self._endpoint

    def node_stage(
        self,
        *,
        volume_id: str,
        staging_target_path: str,
        volume_capability: csi_pb2.VolumeCapability,
        secrets: Mapping[str, Any] | None = None,
        volume_context: Mapping[str, Any] | None = None,
        publish_context: Mapping[str, Any] | None = None,
    ) -> csi_pb2.NodeStageVolumeResponse:
        req = csi_pb2.NodeStageVolumeRequest(
            volume_id=str(volume_id),
            staging_target_path=str(staging_target_path),
            volume_capability=volume_capability,
            secrets=_coalesce_context(secrets),
            volume_context=_coalesce_context(volume_context),
            publish_context=_coalesce_context(publish_context),
        )
        return self._stub.NodeStageVolume(req, timeout=self._timeout)

    def node_publish(
        self,
        *,
        volume_id: str,
        target_path: str,
        staging_target_path: str | None,
        volume_capability: csi_pb2.VolumeCapability,
        read_only: bool,
        secrets: Mapping[str, Any] | None = None,
        volume_context: Mapping[str, Any] | None = None,
        publish_context: Mapping[str, Any] | None = None,
    ) -> csi_pb2.NodePublishVolumeResponse:
        req = csi_pb2.NodePublishVolumeRequest(
            volume_id=str(volume_id),
            target_path=str(target_path),
            volume_capability=volume_capability,
            readonly=bool(read_only),
            secrets=_coalesce_context(secrets),
            volume_context=_coalesce_context(volume_context),
            publish_context=_coalesce_context(publish_context),
            staging_target_path=str(staging_target_path or ""),
        )
        return self._stub.NodePublishVolume(req, timeout=self._timeout)

    def node_unpublish(self, *, volume_id: str, target_path: str) -> csi_pb2.NodeUnpublishVolumeResponse:
        req = csi_pb2.NodeUnpublishVolumeRequest(
            volume_id=str(volume_id),
            target_path=str(target_path),
        )
        return self._stub.NodeUnpublishVolume(req, timeout=self._timeout)

    def node_unstage(
        self, *, volume_id: str, staging_target_path: str
    ) -> csi_pb2.NodeUnstageVolumeResponse:
        req = csi_pb2.NodeUnstageVolumeRequest(
            volume_id=str(volume_id),
            staging_target_path=str(staging_target_path),
        )
        return self._stub.NodeUnstageVolume(req, timeout=self._timeout)
