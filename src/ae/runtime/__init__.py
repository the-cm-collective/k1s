"""Runtime adapters for container operations."""

from .base import RuntimeAdapter, RuntimeResult
from .docker_stub import DockerRuntime

__all__ = ["RuntimeAdapter", "RuntimeResult", "DockerRuntime"]
