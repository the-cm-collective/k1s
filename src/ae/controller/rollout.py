"""Shared rollout manifest mutations."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from ae.controller.spec import AppManifest

RolloutAction = Literal["pause", "resume", "restart"]
RESTART_AT_FIELD = "restartAt"


def restart_timestamp() -> str:
    return datetime.now(UTC).isoformat()


def mutate_rollout_manifest(
    manifest: AppManifest,
    action: RolloutAction,
    *,
    restart_at: str | None = None,
) -> tuple[AppManifest, str | None]:
    rollout = dict(getattr(manifest.spec, "rollout", {}) or {})
    applied_restart_at: str | None = None
    if action == "pause":
        rollout["pause"] = True
    elif action == "resume":
        rollout["pause"] = False
    elif action == "restart":
        applied_restart_at = restart_at or restart_timestamp()
        rollout[RESTART_AT_FIELD] = applied_restart_at
    else:
        raise ValueError(f"unsupported rollout action: {action}")

    new_spec = manifest.spec.model_copy(update={"rollout": rollout})
    return manifest.model_copy(update={"spec": new_spec}), applied_restart_at
