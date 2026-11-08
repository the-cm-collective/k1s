from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from ae.kctl.profile_bootstrap import bootstrap_public_digest
from ae.kctl.profile_host_helper import helper_implementation_digest
from ae.kctl.profile_host_plan import (
    load_strict_cri_host_authorities,
    plan_strict_cri_from_files,
    strict_cri_action_materials,
)
from ae.kctl.profile_node_adapter import (
    ProfileNodeAdapterError,
    StrictCRINodeAdapter,
    StrictCRINodeInputs,
)
from tests.unit.test_kctl_profile_bootstrap import _credential, _store
from tests.unit.test_kctl_profile_host_plan import (
    _facts,
    _intent,
    _write_document,
)
from tests.unit.test_kctl_profile_host_transaction import _Helper, _manager


class _Verifier:
    def __init__(self, observed: tuple[str, ...]) -> None:
        self.observed = observed
        self.calls = 0

    def verify(self, authorities, receipt):
        del authorities, receipt
        self.calls += 1
        return self.observed


def _prepared_node(tmp_path: Path):
    store = _store(tmp_path, "node")
    credential = _credential(tmp_path, "node-token", "private-node-token")
    bootstrap = store.create_node(
        node_id="site-a--node-a",
        controller_url="https://controller.example.test:9443",
        runtime_backend="strict-cri",
        listen_host="127.0.0.1",
        listen_port=9109,
        advertise_endpoint="https://node-a.example.test:9109",
        credential_file=credential,
    )
    intent = _intent(role="node")
    intent["nodeIdentity"] = {
        "bootstrapId": bootstrap.bootstrap_id,
        "bootstrapPublicDigest": bootstrap_public_digest(bootstrap),
        "identityVersion": 1,
        "nodeId": bootstrap.node_id,
    }
    facts = _facts(intent)
    intent_path = tmp_path / "intent.json"
    facts_path = tmp_path / "facts.json"
    intent_digest = _write_document(intent_path, intent)
    facts_digest = _write_document(facts_path, facts)
    plan = plan_strict_cri_from_files(
        intent_path=str(intent_path),
        expected_intent_digest=intent_digest,
        facts_path=str(facts_path),
        expected_facts_digest=facts_digest,
    )
    plan_path = tmp_path / "plan.json"
    plan_file_digest = _write_document(plan_path, plan)
    authorities = load_strict_cri_host_authorities(
        intent_path=str(intent_path),
        expected_intent_digest=intent_digest,
        facts_path=str(facts_path),
        expected_facts_digest=facts_digest,
        plan_path=str(plan_path),
        expected_plan_file_digest=plan_file_digest,
        expected_plan_digest=plan["planDigest"],
    )
    transaction_root = tmp_path / "host-state"
    transaction_root.mkdir(mode=0o700)
    transaction_id = "ht1-" + "4" * 32
    target_id = plan["target"]["targetId"]
    prepared = _manager(_Helper()).prepare(
        intent_path=intent_path,
        expected_intent_digest=intent_digest,
        facts_path=facts_path,
        expected_facts_digest=facts_digest,
        plan_path=plan_path,
        expected_plan_file_digest=plan_file_digest,
        expected_plan_digest=plan["planDigest"],
        transaction_root=transaction_root,
        transaction_id=transaction_id,
        confirmation=f"{target_id}:{plan['planDigest']}",
        expected_helper_identity_digest=helper_implementation_digest(),
    )
    inputs = StrictCRINodeInputs(
        plan_path=plan_path,
        expected_plan_file_digest=plan_file_digest,
        expected_plan_digest=plan["planDigest"],
        receipt_path=transaction_root / "receipts" / f"{transaction_id}.applied.json",
        expected_receipt_digest=prepared.receipt_digest,
    )
    observations = tuple(
        material.desired_after_digest for material in strict_cri_action_materials(authorities)
    )
    return store, bootstrap, authorities, inputs, observations


def test_adapter_binds_bootstrap_plan_receipt_and_exact_post_state(tmp_path: Path) -> None:
    store, bootstrap, _authorities, inputs, observations = _prepared_node(tmp_path)
    verifier = _Verifier(observations)

    authority = StrictCRINodeAdapter(
        bootstrap_store=store,
        verifier=verifier,
        python_executable=Path(sys.executable).resolve(strict=True),
    ).resolve(bootstrap, inputs=inputs)

    assert authority.bootstrap_id == bootstrap.bootstrap_id
    assert authority.node_id == bootstrap.node_id
    assert authority.plan_digest == inputs.expected_plan_digest
    assert authority.receipt_digest == inputs.expected_receipt_digest
    assert dict(authority.child_environment)["AE_AGENT_TOKEN"] == bootstrap.credential
    assert dict(authority.child_environment)["AE_RUNTIME_BACKEND"] == "cri"
    assert verifier.calls == 1


def test_adapter_public_projection_repr_and_digest_hide_private_authority(tmp_path: Path) -> None:
    store, bootstrap, _authorities, inputs, observations = _prepared_node(tmp_path)
    authority = StrictCRINodeAdapter(
        bootstrap_store=store,
        verifier=_Verifier(observations),
        python_executable=Path(sys.executable).resolve(strict=True),
    ).resolve(bootstrap, inputs=inputs)

    rendered = json.dumps(authority.public_projection(), sort_keys=True) + repr(authority)
    for forbidden in (
        bootstrap.credential,
        bootstrap.private_record_digest,
        str(bootstrap.path),
        str(inputs.plan_path),
        str(inputs.receipt_path),
        str(inputs.receipt_path.parent.parent),
    ):
        assert forbidden not in rendered


@pytest.mark.parametrize("drift", ["receipt", "plan", "post-state", "bootstrap"])
def test_adapter_rejects_drift_without_opening_a_runtime(tmp_path: Path, drift: str) -> None:
    store, bootstrap, _authorities, inputs, observations = _prepared_node(tmp_path)
    verifier = _Verifier(observations)
    if drift == "receipt":
        inputs = StrictCRINodeInputs(
            plan_path=inputs.plan_path,
            expected_plan_file_digest=inputs.expected_plan_file_digest,
            expected_plan_digest=inputs.expected_plan_digest,
            receipt_path=inputs.receipt_path,
            expected_receipt_digest="sha256:" + "0" * 64,
        )
    elif drift == "plan":
        inputs = StrictCRINodeInputs(
            plan_path=inputs.plan_path,
            expected_plan_file_digest="sha256:" + "0" * 64,
            expected_plan_digest=inputs.expected_plan_digest,
            receipt_path=inputs.receipt_path,
            expected_receipt_digest=inputs.expected_receipt_digest,
        )
    elif drift == "post-state":
        verifier = _Verifier(("sha256:" + "0" * 64,) * len(observations))
    else:
        replacement = bootstrap.path.with_name("replacement.json")
        replacement.write_bytes(bootstrap.path.read_bytes())
        replacement.chmod(0o600)
        replacement.replace(bootstrap.path)

    adapter = StrictCRINodeAdapter(
        bootstrap_store=store,
        verifier=verifier,
        python_executable=Path(sys.executable).resolve(strict=True),
    )
    with pytest.raises(ProfileNodeAdapterError, match="failed closed"):
        adapter.resolve(bootstrap, inputs=inputs)


def test_adapter_rejects_rolled_back_receipt(tmp_path: Path) -> None:
    store, bootstrap, _authorities, inputs, observations = _prepared_node(tmp_path)
    value = json.loads(inputs.receipt_path.read_text(encoding="utf-8"))
    value["status"] = "rolled-back"
    inputs.receipt_path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    inputs.receipt_path.chmod(0o600)

    with pytest.raises(ProfileNodeAdapterError, match="failed closed"):
        StrictCRINodeAdapter(
            bootstrap_store=store,
            verifier=_Verifier(observations),
            python_executable=Path(sys.executable).resolve(strict=True),
        ).resolve(bootstrap, inputs=inputs)
