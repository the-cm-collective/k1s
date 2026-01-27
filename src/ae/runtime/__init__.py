"""Runtime adapters for container operations."""

from .base import ReplicaState, RuntimeAdapter, RuntimeResult
from .cri_runtime import CRIRuntime
from .docker_runtime import DockerRuntime
from .docker_stub import StubRuntime
from .podman_runtime import PodmanRuntime
from .registry import RegistryAuthProvider
from .remote_runtime import RemoteRuntime

__all__ = [
    "RuntimeAdapter",
    "RuntimeResult",
    "ReplicaState",
    "CRIRuntime",
    "DockerRuntime",
    "PodmanRuntime",
    "StubRuntime",
    "RegistryAuthProvider",
    "RemoteRuntime",
]
