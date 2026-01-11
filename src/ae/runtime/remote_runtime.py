"""Remote runtime shim that delegates RuntimeAdapter calls to an HTTP agent.

This is a minimal skeleton to keep controller/runtime interfaces stable while
we flesh out a real gRPC/HTTP agent. When `agent_url` is None, it falls back to
the provided `local_runtime` to preserve single-node behavior.
"""

from __future__ import annotations

import json
import logging
from typing import Iterable, Optional

import requests

from ae.controller.spec import AppManifest
from .base import ReplicaState, RuntimeAdapter, RuntimeResult

LOGGER = logging.getLogger(__name__)


class RemoteRuntime(RuntimeAdapter):
    """RuntimeAdapter that forwards calls to an ae.node agent over HTTP."""

    def __init__(self, agent_url: str | None, local_runtime: RuntimeAdapter) -> None:
        self._agent_url = agent_url.rstrip("/") if agent_url else None
        self._local = local_runtime
        import os

        self._verify = os.getenv("AE_AGENT_CA_FILE") or True
        cert = os.getenv("AE_AGENT_CERT_FILE")
        key = os.getenv("AE_AGENT_KEY_FILE")
        self._cert = (cert, key) if cert and key else None

    def _use_local(self) -> bool:
        return not self._agent_url

    def _request(self, method: str, path: str, *, timeout: int = 30, **kwargs):
        url = f"{self._agent_url}{path}"
        if self._cert:
            kwargs["cert"] = self._cert
        if self._verify is not True:
            kwargs["verify"] = self._verify
        resp = requests.request(method, url, timeout=timeout, **kwargs)
        resp.raise_for_status()
        return resp

    # --- RuntimeAdapter API --------------------------------------------
    def ensure_app(
        self,
        manifest: AppManifest,
        revision: int,
        *,
        keep_old: bool = False,
        limit_create: int | None = None,
        replica_ids: list[str] | None = None,
        node_id: str | None = None,
    ) -> RuntimeResult:
        if self._use_local():
            return self._local.ensure_app(
                manifest,
                revision,
                keep_old=keep_old,
                limit_create=limit_create,
                replica_ids=replica_ids,
                node_id=node_id,
            )
        payload = {
            "manifest": manifest.model_dump(mode="json", by_alias=True),
            "revision": revision,
            "keep_old": keep_old,
            "limit_create": limit_create,
            "replica_ids": replica_ids,
            "node_id": node_id,
        }
        resp = self._request("POST", "/v1/ensure_app", json=payload, timeout=30)
        data = resp.json()
        return _runtime_result_from_json(data)

    def remove_app(self, app_name: str) -> int:
        if self._use_local():
            return self._local.remove_app(app_name)
        resp = self._request("POST", "/v1/remove_app", json={"app": app_name}, timeout=15)
        return int(resp.json().get("removed", 0))

    def remove_old_revisions(self, app_name: str, keep_revision: int) -> int:
        if self._use_local():
            return self._local.remove_old_revisions(app_name, keep_revision)
        resp = self._request(
            "POST",
            "/v1/remove_old",
            json={"app": app_name, "keep_revision": keep_revision},
            timeout=15,
        )
        return int(resp.json().get("removed", 0))

    def list_containers_info(self) -> list[dict]:
        if self._use_local():
            return self._local.list_containers_info()
        resp = self._request("GET", "/v1/containers", timeout=10)
        return resp.json().get("containers", [])

    def read_logs(
        self,
        replica_id: str,
        *,
        follow: bool = False,
        tail: int | None = None,
        since: int | None = None,
    ):
        if self._use_local():
            return self._local.read_logs(replica_id, follow=follow, tail=tail, since=since)
        params = {"replica_id": replica_id, "follow": follow, "tail": tail, "since": since}
        resp = self._request("GET", "/v1/logs", params=params, timeout=30)
        lines = resp.json().get("lines", [])
        return iter(lines)

    def exec(self, replica_id: str, command: list[str], *, timeout: int | None = None) -> int:
        if self._use_local():
            return self._local.exec(replica_id, command, timeout=timeout)
        payload = {"replica_id": replica_id, "command": command, "timeout": timeout}
        resp = self._request("POST", "/v1/exec", json=payload, timeout=timeout or 30)
        return int(resp.json().get("exit_code", 1))

    def ensure_storage_volumes(self, app_name: str, volumes: list[dict]) -> None:
        if self._use_local():
            return self._local.ensure_storage_volumes(app_name, volumes)
        self._request(
            "POST",
            "/v1/ensure_volumes",
            json={"app": app_name, "volumes": volumes},
            timeout=20,
        )

    def remove_storage_volumes(self, app_name: str, names: list[str]) -> int:
        if self._use_local():
            return self._local.remove_storage_volumes(app_name, names)
        resp = self._request(
            "POST",
            "/v1/remove_volumes",
            json={"app": app_name, "names": names},
            timeout=20,
        )
        return int(resp.json().get("removed", 0))

    def list_storage_volumes(self, app_name: str | None = None) -> list[dict]:
        if self._use_local():
            return self._local.list_storage_volumes(app_name)
        resp = self._request("GET", "/v1/volumes", params={"app": app_name}, timeout=10)
        return resp.json().get("volumes", [])


def _runtime_result_from_json(data: dict) -> RuntimeResult:
    reps = []
    for item in data.get("replica_states", []):
        reps.append(
            ReplicaState(
                replica_id=item.get("replica_id", ""),
                ready=bool(item.get("ready")),
                status=item.get("status", "unknown"),
                endpoint=item.get("endpoint"),
            )
        )
    return RuntimeResult(
        revision=int(data.get("revision", 0)),
        created=int(data.get("created", 0)),
        updated=int(data.get("updated", 0)),
        removed=int(data.get("removed", 0)),
        replica_states=reps,
    )
