#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import sys
from datetime import date, datetime
from pathlib import Path


README_PATH = Path("README.md")
STATUS_PATH = Path("docs/site/k8s_status.json")


def die(message: str) -> None:
    print(f"matrix-drift: {message}", file=sys.stderr)
    sys.exit(1)


def read_matrix_date(text: str) -> date:
    match = re.search(r"^Matrix updated:\s*(\d{4}-\d{2}-\d{2})", text, re.MULTILINE)
    if not match:
        die("missing 'Matrix updated: YYYY-MM-DD' line in README.md")
    return date.fromisoformat(match.group(1))


def read_report_date(payload: dict) -> date:
    generated_at = payload.get("generated_at")
    if not generated_at:
        die("docs/site/k8s_status.json missing generated_at")
    if generated_at.endswith("Z"):
        generated_at = generated_at.replace("Z", "+00:00")
    return datetime.fromisoformat(generated_at).date()


def main() -> None:
    if not README_PATH.exists():
        die("README.md not found")
    if not STATUS_PATH.exists():
        die("docs/site/k8s_status.json not found")

    readme_text = README_PATH.read_text(encoding="utf-8")
    matrix_date = read_matrix_date(readme_text)

    status_payload = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    report_date = read_report_date(status_payload)

    if matrix_date < report_date:
        die(
            f"README matrix is stale: {matrix_date} < report {report_date}. "
            "Update the matrix (and Matrix updated date)."
        )

    print(f"matrix-drift: ok (matrix {matrix_date} >= report {report_date})")


if __name__ == "__main__":
    main()
