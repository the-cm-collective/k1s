"""Runtime adapters for container operations."""

from .base import PodState, ReplicaState, RuntimeAdapter, RuntimeResult
from .docker_runtime import DockerRuntime
from .docker_stub import StubRuntime
from .podman_runtime import PodmanRuntime
from .registry import RegistryAuthProvider
from .remote_runtime import RemoteRuntime

try:
    from .cri_runtime import CRIRuntime
except Exception as exc:  # pragma: no cover - optional dependency
    _cri_exc_msg = str(exc)

    class _MissingCRIRuntime:  # type: ignore[too-many-public-methods]
        def __init__(self, *args, **kwargs) -> None:  # noqa: D401 - simple wrapper
            raise RuntimeError(f"CRIRuntime unavailable: {_cri_exc_msg}")

    CRIRuntime = _MissingCRIRuntime  # type: ignore[assignment]

__all__ = [
    "RuntimeAdapter",
    "RuntimeResult",
    "PodState",
    "ReplicaState",
    "CRIRuntime",
    "DockerRuntime",
    "PodmanRuntime",
    "StubRuntime",
    "RegistryAuthProvider",
    "RemoteRuntime",
]
