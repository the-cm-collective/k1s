"""Installed strict-CRI node execution authority.

This module validates explicit source authorities and produces a secret-safe
descriptor.  It does not open the CRI endpoint or start a component.
"""

from __future__ import annotations

import hashlib
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from ae.kctl.profile_bootstrap import (
    ProfileBootstrapRecord,
    ProfileBootstrapStore,
    bootstrap_public_digest,
)
from ae.kctl.profile_host_plan import (
    StrictCRIHostAuthorities,
    canonical_json_bytes,
    load_strict_cri_host_authorities,
    strict_cri_action_materials,
)
from ae.kctl.profile_host_transaction import (
    StrictCRIHostReceipt,
    load_strict_cri_host_receipt,
)


class ProfileNodeAdapterError(RuntimeError):
    """The explicit bootstrap/plan/receipt authority is inconsistent."""


def _fail() -> ProfileNodeAdapterError:
    return ProfileNodeAdapterError("strict CRI node adapter failed closed")


class HostPostStateVerifier(Protocol):
    def verify(
        self,
        authorities: StrictCRIHostAuthorities,
        receipt: StrictCRIHostReceipt,
    ) -> tuple[str, ...]:
        """Return exact current action observation digests without socket I/O."""


@dataclass(frozen=True, slots=True)
class StrictCRINodeInputs:
    plan_path: Path = field(repr=False)
    expected_plan_file_digest: str
    expected_plan_digest: str
    receipt_path: Path = field(repr=False)
    expected_receipt_digest: str


@dataclass(frozen=True, slots=True)
class StrictCRINodeAuthority:
    bootstrap_id: str
    node_id: str
    controller_url: str
    listen_host: str
    listen_port: int
    advertise_endpoint: str
    cri_endpoint: str
    runtime_handler: str
    plan_digest: str
    receipt_digest: str
    execution_digest: str
    child_environment: tuple[tuple[str, str], ...] = field(repr=False)

    def public_projection(self) -> dict[str, object]:
        return {
            "advertiseEndpoint": self.advertise_endpoint,
            "bootstrapId": self.bootstrap_id,
            "controllerUrl": self.controller_url,
            "criEndpoint": self.cri_endpoint,
            "executionDigest": self.execution_digest,
            "listenHost": self.listen_host,
            "listenPort": self.listen_port,
            "nodeId": self.node_id,
            "planDigest": self.plan_digest,
            "qualificationPosture": "source-validated/unqualified",
            "receiptDigest": self.receipt_digest,
            "runtimeHandler": self.runtime_handler,
        }


class StrictCRINodeAdapter:
    def __init__(
        self,
        *,
        bootstrap_store: ProfileBootstrapStore,
        verifier: HostPostStateVerifier,
        python_executable: Path,
    ) -> None:
        self.bootstrap_store = bootstrap_store
        self.verifier = verifier
        self.python_executable = python_executable

    def resolve(
        self,
        bootstrap: ProfileBootstrapRecord,
        *,
        inputs: StrictCRINodeInputs,
    ) -> StrictCRINodeAuthority:
        try:
            current = self.bootstrap_store.revalidate(bootstrap)
            if current.profile != "node" or current.backend_id != "strict-cri":
                raise _fail()
            receipt = load_strict_cri_host_receipt(
                inputs.receipt_path,
                expected_digest=inputs.expected_receipt_digest,
            )
            receipt_name = f"{receipt.transaction_id}.applied.json"
            if (
                inputs.receipt_path.name != receipt_name
                or inputs.receipt_path.parent.name != "receipts"
            ):
                raise _fail()
            transaction_root = inputs.receipt_path.parent.parent
            authority_dir = (
                transaction_root / "transactions" / receipt.transaction_id / "authorities"
            )
            authorities = load_strict_cri_host_authorities(
                intent_path=str(authority_dir / "intent.json"),
                expected_intent_digest=receipt.intent_digest,
                facts_path=str(authority_dir / "facts.json"),
                expected_facts_digest=receipt.facts_digest,
                plan_path=str(inputs.plan_path),
                expected_plan_file_digest=inputs.expected_plan_file_digest,
                expected_plan_digest=inputs.expected_plan_digest,
            )
            plan = authorities.plan
            node = plan["nodeIdentity"]
            if (
                plan["role"] != "node"
                or type(node) is not dict
                or node.get("identityVersion") != 1
                or node.get("nodeId") != current.node_id
                or node.get("bootstrapId") != current.bootstrap_id
                or node.get("bootstrapPublicDigest") != bootstrap_public_digest(current)
            ):
                raise _fail()
            if (
                receipt.status != "applied"
                or receipt.target_id != plan["target"]["targetId"]
                or receipt.plan_digest != authorities.plan_digest
                or receipt.plan_file_digest != authorities.plan_file_digest
                or receipt.intent_digest != authorities.intent_digest
                or receipt.facts_digest != authorities.facts_digest
            ):
                raise _fail()
            expected_observations = tuple(
                material.desired_after_digest
                for material in strict_cri_action_materials(authorities)
            )
            observed = self.verifier.verify(authorities, receipt)
            if (
                observed != expected_observations
                or receipt.action_observation_digests != expected_observations
            ):
                raise _fail()
            executable_identity = _executable_identity(self.python_executable)
            payload = {
                "adapterVersion": 1,
                "bootstrap": current.public_projection(),
                "componentOrder": ["node-agent"],
                "criEndpoint": plan["cri"]["endpoint"],
                "planDigest": receipt.plan_digest,
                "pythonExecutableIdentity": executable_identity,
                "receiptDigest": receipt.receipt_digest,
                "runtimeHandler": plan["containerd"]["runtimeHandler"],
            }
            execution_digest = f"sha256:{hashlib.sha256(canonical_json_bytes(payload)).hexdigest()}"
            assert current.controller_url is not None
            assert current.listen_host is not None
            assert current.listen_port is not None
            assert current.advertise_endpoint is not None
            environment = {
                "AE_AGENT_TOKEN": current.credential,
                "AE_CONTROLLER_URL": current.controller_url,
                "AE_CRI_ENDPOINT": plan["cri"]["endpoint"],
                "AE_NODE_ID": current.node_id,
                "AE_RUNTIME_BACKEND": "cri",
                "LC_ALL": "C.UTF-8",
                "PYTHONUNBUFFERED": "1",
                "TZ": "UTC",
            }
            return StrictCRINodeAuthority(
                bootstrap_id=current.bootstrap_id,
                node_id=current.node_id,
                controller_url=current.controller_url,
                listen_host=current.listen_host,
                listen_port=current.listen_port,
                advertise_endpoint=current.advertise_endpoint,
                cri_endpoint=plan["cri"]["endpoint"],
                runtime_handler=plan["containerd"]["runtimeHandler"],
                plan_digest=receipt.plan_digest,
                receipt_digest=receipt.receipt_digest,
                execution_digest=execution_digest,
                child_environment=tuple(sorted(environment.items())),
            )
        except ProfileNodeAdapterError:
            raise
        except Exception as exc:
            raise _fail() from exc


def _executable_identity(path: Path) -> dict[str, object]:
    if not path.is_absolute():
        raise _fail()
    try:
        info = path.stat()
    except OSError as exc:
        raise _fail() from exc
    if not stat.S_ISREG(info.st_mode) or info.st_ino <= 0:
        raise _fail()
    return {"device": info.st_dev, "inode": info.st_ino, "path": str(path)}


__all__ = [
    "HostPostStateVerifier",
    "ProfileNodeAdapterError",
    "StrictCRINodeAdapter",
    "StrictCRINodeAuthority",
    "StrictCRINodeInputs",
]
