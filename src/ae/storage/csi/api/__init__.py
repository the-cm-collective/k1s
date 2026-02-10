"""Generated CSI protobuf modules."""

from . import csi_pb2

try:  # pragma: no cover - optional dependency
    from . import csi_pb2_grpc
except Exception:  # pragma: no cover
    csi_pb2_grpc = None

__all__ = ["csi_pb2", "csi_pb2_grpc"]
