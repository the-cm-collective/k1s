#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    from tests.e2e.core_edge import run_core_edge_e2e

    return int(run_core_edge_e2e())


if __name__ == "__main__":
    raise SystemExit(main())
