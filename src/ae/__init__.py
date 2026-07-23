"""ae package root for the minimal application engine."""

__version__ = "0.1.6.dev3"


def build_info() -> dict:
    """Return build metadata (version, sha, date) from environment if available."""
    import os

    return {
        "version": __version__,
        "sha": os.getenv("AE_BUILD_SHA", "unknown"),
        "date": os.getenv("AE_BUILD_DATE", "unknown"),
    }
