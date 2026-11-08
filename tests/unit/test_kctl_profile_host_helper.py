from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

import pytest

from ae.kctl.profile_host_helper import (
    HelperAuthorityFiles,
    HelperRequest,
    ProfileHostHelperError,
    StrictCRIHostHelperEngine,
    helper_implementation_digest,
    read_protocol_message,
    write_protocol_message,
)
from ae.kctl.profile_host_plan import (
    load_strict_cri_host_authorities,
    strict_cri_action_materials,
)
from tests.unit.test_kctl_profile_host_plan import _digest, _from_files, _write_document


class _Boundary:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int, str]] = []
        self.override: str | None = None

    def execute(self, *, operation, transaction_id, transaction_root_fd, material, **kwargs):
        assert os.fstat(transaction_root_fd).st_ino > 0
        del kwargs
        self.calls.append((operation, material.index, transaction_id))
        if self.override is not None:
            return self.override
        return {
            "snapshot": material.expected_before_digest,
            "apply": material.desired_after_digest,
            "rollback": material.expected_before_digest,
            "verify": material.desired_after_digest,
        }[operation]


def _bundle(tmp_path: Path):
    _intent, _facts, plan, intent_path, facts_path = _from_files(tmp_path, role="node")
    plan_path = tmp_path / "plan.json"
    plan_file_digest = _write_document(plan_path, plan)
    authorities = load_strict_cri_host_authorities(
        intent_path=str(intent_path),
        expected_intent_digest=_digest(intent_path.read_bytes()),
        facts_path=str(facts_path),
        expected_facts_digest=_digest(facts_path.read_bytes()),
        plan_path=str(plan_path),
        expected_plan_file_digest=plan_file_digest,
        expected_plan_digest=plan["planDigest"],
    )
    root = tmp_path / "helper-private-host-state"
    root.mkdir(mode=0o700)
    txid = "ht1-" + "1" * 32
    authority_dir = root / "transactions" / txid / "authorities"
    authority_dir.mkdir(mode=0o700, parents=True)
    (authority_dir.parent / "backups").mkdir(mode=0o700)
    (root / "receipts").mkdir(mode=0o700)
    for directory in (root / "transactions", authority_dir.parent, authority_dir):
        directory.chmod(0o700)
    persisted_paths = tuple(
        authority_dir / name for name in ("intent.json", "facts.json", "plan.json")
    )
    for source, target in zip(
        (intent_path, facts_path, plan_path),
        persisted_paths,
        strict=True,
    ):
        target.write_bytes(source.read_bytes())
        target.chmod(0o600)
    material = strict_cri_action_materials(authorities)[0]
    request = HelperRequest(
        operation="snapshot",
        transaction_id=txid,
        target_id=plan["target"]["targetId"],
        action_index=material.index,
        action_id=material.action_id,
        expected_before_digest=material.expected_before_digest,
        desired_after_digest=material.desired_after_digest,
        authorities=HelperAuthorityFiles(
            intent_path=persisted_paths[0],
            facts_path=persisted_paths[1],
            plan_path=persisted_paths[2],
            intent_digest=authorities.intent_digest,
            facts_digest=authorities.facts_digest,
            plan_file_digest=authorities.plan_file_digest,
            plan_digest=authorities.plan_digest,
        ),
        transaction_root=root,
        helper_identity_digest=helper_implementation_digest(),
    )
    return authorities, request


def test_helper_reconstructs_authority_and_derives_exact_ordered_action(tmp_path: Path) -> None:
    authorities, request = _bundle(tmp_path)
    boundary = _Boundary()

    response = StrictCRIHostHelperEngine(boundary).handle(request)

    material = strict_cri_action_materials(authorities)[0]
    assert response.action_id == material.action_id
    assert response.expected_before_digest == material.expected_before_digest
    assert response.desired_after_digest == material.desired_after_digest
    assert response.observation_digest == material.expected_before_digest
    assert boundary.calls == [("snapshot", 0, request.transaction_id)]


@pytest.mark.parametrize(
    "changed",
    [
        {"action_id": "different-action"},
        {"expected_before_digest": "sha256:" + "0" * 64},
        {"desired_after_digest": "sha256:" + "0" * 64},
        {"target_id": "different-target"},
    ],
)
def test_helper_rejects_coordinator_action_metadata_divergence(
    tmp_path: Path, changed: dict[str, object]
) -> None:
    _authorities, request = _bundle(tmp_path)

    with pytest.raises(ProfileHostHelperError, match="failed closed"):
        StrictCRIHostHelperEngine(_Boundary()).handle(replace(request, **changed))


def test_helper_rejects_plan_that_no_longer_reconstructs_byte_for_byte(tmp_path: Path) -> None:
    _authorities, request = _bundle(tmp_path)
    plan = request.authorities.plan_path.read_bytes()
    divergent = plan.replace(b'"mutating": false', b'"mutating": true')
    request.authorities.plan_path.write_bytes(divergent)
    request.authorities.plan_path.chmod(0o600)
    changed = replace(
        request,
        authorities=replace(
            request.authorities,
            plan_file_digest=_digest(divergent),
        ),
    )

    with pytest.raises(ProfileHostHelperError, match="failed closed"):
        StrictCRIHostHelperEngine(_Boundary()).handle(changed)


def test_helper_rejects_authority_outside_exact_transaction_layout(tmp_path: Path) -> None:
    _authorities, request = _bundle(tmp_path)
    changed = replace(
        request,
        authorities=replace(request.authorities, plan_path=tmp_path / "plan.json"),
    )

    with pytest.raises(ProfileHostHelperError, match="failed closed"):
        StrictCRIHostHelperEngine(_Boundary()).handle(changed)


def test_helper_rejects_unsafe_private_transaction_directory(tmp_path: Path) -> None:
    _authorities, request = _bundle(tmp_path)
    request.transaction_root.chmod(0o755)

    with pytest.raises(ProfileHostHelperError, match="failed closed"):
        StrictCRIHostHelperEngine(_Boundary()).handle(request)


def test_helper_rejects_symlinked_transaction_parent(tmp_path: Path) -> None:
    _authorities, request = _bundle(tmp_path)
    transactions = request.transaction_root / "transactions"
    held = request.transaction_root / "transactions-held"
    transactions.rename(held)
    transactions.symlink_to(held, target_is_directory=True)

    with pytest.raises(ProfileHostHelperError, match="failed closed"):
        StrictCRIHostHelperEngine(_Boundary()).handle(request)


def test_helper_holds_root_descriptor_across_path_parent_replacement(tmp_path: Path) -> None:
    _authorities, request = _bundle(tmp_path)
    original_identity = request.transaction_root.stat().st_ino

    class _ReplacingBoundary(_Boundary):
        def execute(self, *, transaction_root_fd, **kwargs):
            moved = request.transaction_root.with_name("moved-private-host-state")
            request.transaction_root.rename(moved)
            request.transaction_root.mkdir(mode=0o700)
            assert os.fstat(transaction_root_fd).st_ino == original_identity
            assert request.transaction_root.stat().st_ino != original_identity
            return super().execute(transaction_root_fd=transaction_root_fd, **kwargs)

    StrictCRIHostHelperEngine(_ReplacingBoundary()).handle(request)


def test_helper_rejects_divergent_post_observation(tmp_path: Path) -> None:
    _authorities, request = _bundle(tmp_path)
    boundary = _Boundary()
    boundary.override = "sha256:" + "f" * 64

    with pytest.raises(ProfileHostHelperError, match="failed closed"):
        StrictCRIHostHelperEngine(boundary).handle(request)


def test_helper_request_repr_and_public_failure_hide_private_authority(tmp_path: Path) -> None:
    _authorities, request = _bundle(tmp_path)
    rendered = repr(request)
    assert str(request.transaction_root) not in rendered
    assert str(request.authorities.intent_path) not in rendered
    assert request.expected_before_digest not in rendered
    assert request.desired_after_digest not in rendered

    with pytest.raises(ProfileHostHelperError) as captured:
        StrictCRIHostHelperEngine(_Boundary()).handle(replace(request, action_index=999))
    assert str(request.transaction_root) not in str(captured.value)
    assert str(request.authorities.intent_path) not in str(captured.value)


def test_anonymous_pipe_protocol_is_canonical_bounded_and_round_trips() -> None:
    read_fd, write_fd = os.pipe()
    try:
        payload = {"apiVersion": "k1s.dev/v1alpha1", "kind": "Fixture"}
        write_protocol_message(write_fd, payload)
        assert read_protocol_message(read_fd) == payload
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_anonymous_pipe_protocol_rejects_noncanonical_and_duplicate_json() -> None:
    for raw in (
        b'{"b":1,"a":2}\n',
        b'{"a":1,"a":1}\n',
    ):
        read_fd, write_fd = os.pipe()
        try:
            os.write(write_fd, f"{len(raw):08x}\n".encode("ascii") + raw)
            with pytest.raises(ProfileHostHelperError, match="failed closed"):
                read_protocol_message(read_fd)
        finally:
            os.close(read_fd)
            os.close(write_fd)


def test_helper_identity_is_explicitly_only_a_compatibility_binding() -> None:
    first = helper_implementation_digest()
    second = helper_implementation_digest()
    assert first == second
    assert first.startswith("sha256:")
    assert "authenticate" not in StrictCRIHostHelperEngine.__doc__.lower()
