"""Provider interface for Service/overlay networking."""

from __future__ import annotations

from typing import Dict, List, Protocol, Tuple


class NetworkProvider(Protocol):
    """Abstracts dataplane operations for Service VIP routing."""

    def ensure_network(self) -> None:
        """Ensure base network constructs exist (bridge/overlay/iptables)."""

    def ensure_service(self, app_name: str, ports: dict) -> str:
        """Ensure a Service is present and return its ClusterIP."""

    def update_service_endpoints(
        self, app_name: str, backends_by_port: Dict[int, List[Tuple[str, int]]]
    ) -> None:
        """Update ready backends keyed by Service port."""

    def remove_service(self, app_name: str) -> None:
        """Remove Service dataplane artifacts."""


class NullProvider:
    """No-op provider used as a placeholder before real dataplane wiring."""

    def ensure_network(self) -> None:  # pragma: no cover - trivial
        return

    def ensure_service(self, app_name: str, ports: dict) -> str:  # pragma: no cover - trivial
        return "127.0.0.1"

    def update_service_endpoints(
        self, app_name: str, backends_by_port: Dict[int, List[Tuple[str, int]]]
    ) -> None:  # pragma: no cover - trivial
        return

    def remove_service(self, app_name: str) -> None:  # pragma: no cover - trivial
        return


# ruff: noqa: E501,UP006,UP007,UP017,UP035,ARG002
