"""Network helpers for Service VIP and multi-node plumbing."""

from .provider import NetworkProvider, NullProvider
from .provider_docker import DockerBridgeProvider
from .provider_overlay import OverlayProvider
from .service_controller import ServiceController
try:
    from .pod_cidr import PodCIDRAllocator
except Exception:  # pragma: no cover
    PodCIDRAllocator = None  # type: ignore

__all__ = [
    "NetworkProvider",
    "NullProvider",
    "DockerBridgeProvider",
    "OverlayProvider",
    "ServiceController",
    "PodCIDRAllocator",
]
