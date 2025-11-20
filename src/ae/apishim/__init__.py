"""Kubernetes API shim for k1s (phase 0).

Exposes minimal discovery endpoints and CRUD for core/v1 objects so kubectl/helm
can interact with a k1s-backed store. See docs/design/api-shim.md.
"""

__all__ = [
    "serve",
]

