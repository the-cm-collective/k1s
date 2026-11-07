from __future__ import annotations

import copy
import hashlib
import os
from pathlib import Path

import pytest

from ae.kctl import profile_host_plan as host_plan
from ae.kctl.profile_host_plan import (
    ProfileHostPlanError,
    build_strict_cri_host_plan,
    canonical_json_bytes,
    plan_strict_cri_from_files,
    render_strict_cri_host_plan_json,
    render_strict_cri_host_plan_text,
)


def _digest(raw: bytes) -> str:
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


def _value_digest(value: object) -> str:
    return _digest(canonical_json_bytes(value))


def _present(
    path: str,
    object_type: str,
    *,
    inode: int,
    digest: str | None = None,
    mode: str | None = None,
) -> dict[str, object]:
    if mode is None:
        mode = {"directory": "0755", "regular": "0755", "socket": "0660"}[object_type]
    return {
        "device": 7,
        "gid": 0,
        "inode": inode,
        "mode": mode,
        "nlink": 1 if object_type != "directory" else 2,
        "path": path,
        "sha256": digest if object_type == "regular" else None,
        "size": 4096 if object_type == "regular" else 0,
        "state": "present",
        "type": object_type,
        "uid": 0,
    }


def _absent(path: str, *, inode: int) -> dict[str, object]:
    return {
        "parent": {
            "device": 7,
            "gid": 0,
            "inode": inode,
            "mode": "0755",
            "nlink": 2,
            "path": str(Path(path).parent),
            "type": "directory",
            "uid": 0,
        },
        "path": path,
        "state": "absent",
    }


def _intent(*, role: str = "core", registry: bool = False) -> dict[str, object]:
    node_identity: dict[str, object] | None = None
    profile_id = "core-a"
    if role == "node":
        profile_id = "node-a"
        node_identity = {
            "bootstrapId": "b1-1234567890abcdef",
            "bootstrapPublicDigest": "sha256:" + "b" * 64,
            "identityVersion": 1,
            "nodeId": "site-a--node-a",
        }
    registry_value: dict[str, object] = {"mode": "off"}
    if registry:
        registry_value = {
            "caObject": "registry-ca-source",
            "caSha256": "sha256:" + "c" * 64,
            "host": "registry.example.invalid:5443",
            "mode": "external-tls",
            "scheme": "https",
            "systemTrust": False,
        }
    return {
        "apiVersion": "k1s.dev/v1alpha1",
        "cni": {
            "bridgeName": "k1s0",
            "configRoot": "/etc/cni/net.d",
            "cniVersion": "1.0.0",
            "podCidr": "10.42.0.0/16",
            "requiredPlugins": ["bridge", "portmap", "firewall", "tuning", "loopback"],
            "sourceBinRoot": "/opt/k1s/cni-source",
            "targetBinRoot": "/opt/cni/bin",
        },
        "containerd": {
            "certsRoot": "/etc/containerd/certs.d",
            "configPath": "/etc/containerd/config.toml",
        },
        "cri": {
            "endpoint": "unix:///run/containerd/containerd.sock",
            "runtimeHandler": "runc",
        },
        "kind": "StrictCRIHostIntent",
        "nodeIdentity": node_identity,
        "profileId": profile_id,
        "registry": registry_value,
        "role": role,
        "schemaVersion": 1,
        "socketAccess": {"gid": 1000, "mode": "acl", "uid": 1000},
        "target": {
            "hostIdentitySha256": "sha256:" + "a" * 64,
            "ownerGid": 1000,
            "ownerUid": 1000,
            "targetId": "guest-a",
        },
    }


def _facts(intent: dict[str, object]) -> dict[str, object]:
    cni = intent["cni"]
    containerd = intent["containerd"]
    cri = intent["cri"]
    assert isinstance(cni, dict)
    assert isinstance(containerd, dict)
    assert isinstance(cri, dict)
    socket_path = str(cri["endpoint"])[len("unix://") :]
    objects: dict[str, object] = {
        "criSocket": _present(socket_path, "socket", inode=1),
        "containerdConfig": _present(
            str(containerd["configPath"]),
            "regular",
            inode=2,
            digest="sha256:" + "1" * 64,
            mode="0644",
        ),
        "containerdCertsRoot": _present(str(containerd["certsRoot"]), "directory", inode=3),
        "cniSourceRoot": _present(str(cni["sourceBinRoot"]), "directory", inode=4),
        "cniTargetRoot": _present(str(cni["targetBinRoot"]), "directory", inode=5),
        "cniConfigRoot": _present(str(cni["configRoot"]), "directory", inode=6),
    }
    for offset, plugin in enumerate(host_plan.REQUIRED_CNI_PLUGINS, 10):
        source_path = f"{cni['sourceBinRoot']}/{plugin}"
        target_path = f"{cni['targetBinRoot']}/{plugin}"
        objects[f"cniSource:{plugin}"] = _present(
            source_path,
            "regular",
            inode=offset,
            digest=f"sha256:{offset:064x}",
        )
        objects[f"cniTarget:{plugin}"] = _absent(target_path, inode=5)
    for name in host_plan.CONFIG_TARGETS:
        objects[f"cniConfig:{name}"] = _absent(f"{cni['configRoot']}/{name}", inode=6)
    registry = intent["registry"]
    assert isinstance(registry, dict)
    if registry["mode"] == "external-tls":
        root = f"{containerd['certsRoot']}/{registry['host']}"
        objects[str(registry["caObject"])] = _present(
            "/opt/k1s/trust/registry-ca.crt",
            "regular",
            inode=40,
            digest=str(registry["caSha256"]),
            mode="0644",
        )
        objects["registryRoot"] = _present(root, "directory", inode=41)
        objects["registryCaTarget"] = _absent(f"{root}/ca.crt", inode=41)
        objects["registryHostsTarget"] = _absent(f"{root}/hosts.toml", inode=41)
    target = intent["target"]
    assert isinstance(target, dict)
    return {
        "apiVersion": "k1s.dev/v1alpha1",
        "arch": "amd64",
        "containerd": {
            "configActive": True,
            "criApiVersion": "v1",
            "runtimeHandlers": ["runc"],
            "version": "1.7.24",
        },
        "hostIdentitySha256": target["hostIdentitySha256"],
        "kind": "StrictCRIHostFacts",
        "objects": objects,
        "ownerGid": target["ownerGid"],
        "ownerUid": target["ownerUid"],
        "platform": "linux",
        "rootless": False,
        "schemaVersion": 1,
        "targetId": target["targetId"],
    }


def _write_document(path: Path, value: object) -> str:
    raw = canonical_json_bytes(value)
    path.write_bytes(raw)
    path.chmod(0o600)
    return _digest(raw)


def _from_files(tmp_path: Path, *, role: str = "core", registry: bool = False):
    intent = _intent(role=role, registry=registry)
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
    return intent, facts, plan, intent_path, facts_path


def test_canonical_plan_is_deterministic_source_validated_and_nonmutating(tmp_path: Path) -> None:
    _intent_value, _facts_value, first, intent_path, facts_path = _from_files(tmp_path)
    before = {
        item.name: (item.stat().st_mode, item.stat().st_size, item.stat().st_mtime_ns)
        for item in tmp_path.iterdir()
    }
    second = plan_strict_cri_from_files(
        intent_path=str(intent_path),
        expected_intent_digest=_digest(intent_path.read_bytes()),
        facts_path=str(facts_path),
        expected_facts_digest=_digest(facts_path.read_bytes()),
    )
    after = {
        item.name: (item.stat().st_mode, item.stat().st_size, item.stat().st_mtime_ns)
        for item in tmp_path.iterdir()
    }
    assert first == second
    assert canonical_json_bytes(first) == canonical_json_bytes(second)
    assert before == after
    assert set(before) == {"intent.json", "facts.json"}
    assert first["mutating"] is False
    assert first["qualificationPosture"] == "source-validated/unqualified"
    digest_projection = dict(first)
    assert digest_projection.pop("planDigest") == _value_digest(digest_projection)


def test_plan_actions_are_closed_ordered_and_prepare_no_services(tmp_path: Path) -> None:
    _intent_value, _facts_value, plan, _intent_path, _facts_path = _from_files(tmp_path)
    assert [action["id"] for action in plan["actions"]] == [
        "validate-cri-socket",
        "validate-containerd-config",
        "cni-plugin-bridge",
        "cni-plugin-portmap",
        "cni-plugin-firewall",
        "cni-plugin-tuning",
        "cni-plugin-loopback",
        "cni-config-10-k1s.conflist",
        "cni-config-99-loopback.conf",
        "cri-socket-access",
    ]
    assert all(
        set(action)
        == {"desiredAfterDigest", "expectedBeforeDigest", "id", "kind", "rollback", "target"}
        for action in plan["actions"]
    )
    rendered = render_strict_cri_host_plan_json(plan)
    for forbidden in ("sudo", "systemctl", "restart", "microk8s", "iptables", "credential"):
        assert forbidden not in rendered.lower()


def test_node_identity_v1_is_public_but_private_authority_is_absent(tmp_path: Path) -> None:
    _intent_value, _facts_value, plan, intent_path, facts_path = _from_files(tmp_path, role="node")
    rendered = render_strict_cri_host_plan_json(plan)
    assert plan["nodeIdentity"] == {
        "bootstrapId": "b1-1234567890abcdef",
        "bootstrapPublicDigest": "sha256:" + "b" * 64,
        "identityVersion": 1,
        "nodeId": "site-a--node-a",
    }
    assert str(intent_path) not in rendered
    assert str(facts_path) not in rendered
    assert "privateRecordDigest" not in rendered
    assert "credential" not in rendered.lower()


def test_external_tls_registry_has_exact_noninsecure_actions(tmp_path: Path) -> None:
    _intent_value, _facts_value, plan, _intent_path, _facts_path = _from_files(
        tmp_path, registry=True
    )
    actions = {item["id"]: item for item in plan["actions"]}
    assert actions["registry-ca"]["kind"] == "copy-regular"
    assert actions["registry-hosts"]["kind"] == "write-canonical"
    assert plan["registry"]["scheme"] == "https"
    assert plan["registry"]["systemTrust"] is False
    assert "skip" not in render_strict_cri_host_plan_json(plan).lower()


@pytest.mark.parametrize(
    ("socket_uid", "socket_gid", "socket_mode"),
    [(1000, 0, "0600"), (0, 1000, "0660")],
)
def test_property_existing_socket_access_requires_exact_owner_or_group_rw(
    socket_uid: int, socket_gid: int, socket_mode: str
) -> None:
    intent = _intent()
    intent["socketAccess"] = {"gid": 1000, "mode": "existing", "uid": 1000}
    facts = _facts(intent)
    socket = facts["objects"]["criSocket"]
    socket.update({"gid": socket_gid, "mode": socket_mode, "uid": socket_uid})

    plan = build_strict_cri_host_plan(
        intent,
        facts,
        intent_digest=_value_digest(intent),
        facts_digest=_value_digest(facts),
    )

    actions = {item["id"]: item for item in plan["actions"]}
    assert actions["cri-socket-access"]["kind"] == "verify"


@pytest.mark.parametrize(
    ("socket_uid", "socket_gid", "socket_mode"),
    [
        (0, 0, "0600"),
        (1000, 0, "0200"),
        (0, 1000, "0640"),
        (0, 0, "0666"),
    ],
)
def test_property_existing_socket_access_rejects_unproved_or_world_access(
    socket_uid: int, socket_gid: int, socket_mode: str
) -> None:
    intent = _intent()
    intent["socketAccess"] = {"gid": 1000, "mode": "existing", "uid": 1000}
    facts = _facts(intent)
    socket = facts["objects"]["criSocket"]
    socket.update({"gid": socket_gid, "mode": socket_mode, "uid": socket_uid})

    with pytest.raises(ProfileHostPlanError, match="^strict CRI host plan failed closed$"):
        build_strict_cri_host_plan(
            intent,
            facts,
            intent_digest=_value_digest(intent),
            facts_digest=_value_digest(facts),
        )


def test_acl_socket_access_still_emits_exact_acl_for_inaccessible_socket() -> None:
    intent = _intent()
    facts = _facts(intent)
    socket = facts["objects"]["criSocket"]
    socket.update({"gid": 0, "mode": "0600", "uid": 0})

    plan = build_strict_cri_host_plan(
        intent,
        facts,
        intent_digest=_value_digest(intent),
        facts_digest=_value_digest(facts),
    )

    actions = {item["id"]: item for item in plan["actions"]}
    assert actions["cri-socket-access"]["kind"] == "grant-exact-acl"


def test_text_render_is_deterministic_and_carries_the_same_plan_digest(tmp_path: Path) -> None:
    _intent_value, _facts_value, plan, _intent_path, _facts_path = _from_files(tmp_path)
    first = render_strict_cri_host_plan_text(plan)
    second = render_strict_cri_host_plan_text(plan)
    assert first == second
    assert first.endswith("\n")
    assert f"planDigest={plan['planDigest']}" in first
    assert "qualification=source-validated/unqualified" in first


@pytest.mark.parametrize(
    ("mutator", "document"),
    [
        (lambda value: value.update({"unexpected": True}), "intent"),
        (lambda value: value.update({"role": "edge"}), "intent"),
        (lambda value: value["cri"].update({"endpoint": "tcp://127.0.0.1:1234"}), "intent"),
        (lambda value: value["cri"].update({"endpoint": "unix:///run//containerd.sock"}), "intent"),
        (lambda value: value["cni"].update({"podCidr": "10.42.0.1/16"}), "intent"),
        (lambda value: value["cni"]["requiredPlugins"].reverse(), "intent"),
        (lambda value: value["socketAccess"].update({"uid": 1001}), "intent"),
        (lambda value: value.update({"nodeIdentity": {}}), "intent"),
        (lambda value: value.update({"platform": "darwin"}), "facts"),
        (lambda value: value.update({"rootless": True}), "facts"),
        (lambda value: value["containerd"].update({"configActive": False}), "facts"),
        (lambda value: value["containerd"].update({"runtimeHandlers": ["other"]}), "facts"),
        (lambda value: value["objects"]["criSocket"].update({"type": "regular"}), "facts"),
        (lambda value: value["objects"]["containerdConfig"].update({"nlink": 2}), "facts"),
        (lambda value: value["objects"]["cniSourceRoot"].update({"mode": "0777"}), "facts"),
        (
            lambda value: value["objects"]["cniTarget:bridge"]["parent"].update({"inode": 999}),
            "facts",
        ),
        (lambda value: value["objects"].update({"ambient": {}}), "facts"),
    ],
)
def test_property_invalid_or_ambiguous_contracts_fail_closed(mutator, document: str) -> None:
    intent = _intent()
    facts = _facts(intent)
    target = intent if document == "intent" else facts
    mutator(target)
    with pytest.raises(ProfileHostPlanError, match="^strict CRI host plan failed closed$"):
        build_strict_cri_host_plan(
            intent,
            facts,
            intent_digest=_value_digest(intent),
            facts_digest=_value_digest(facts),
        )


@pytest.mark.parametrize(
    "raw",
    [
        b'{"duplicate": 1, "duplicate": 2}\n',
        b'{"bad": NaN}\n',
        b"\xff\n",
        b'{"nul": "\x00"}\n',
        b'{"carriage": true}\r\n',
        b' {"noncanonical":true}\n',
    ],
)
def test_canonical_input_negative_cases_are_redacted(tmp_path: Path, raw: bytes) -> None:
    intent = tmp_path / "private-intent.json"
    facts_value = _facts(_intent())
    facts = tmp_path / "facts.json"
    intent.write_bytes(raw)
    intent.chmod(0o600)
    facts_digest = _write_document(facts, facts_value)
    with pytest.raises(ProfileHostPlanError) as raised:
        plan_strict_cri_from_files(
            intent_path=str(intent),
            expected_intent_digest=_digest(raw),
            facts_path=str(facts),
            expected_facts_digest=facts_digest,
        )
    assert str(raised.value) == "strict CRI host plan failed closed"
    assert str(intent) not in str(raised.value)


def test_symlink_and_hardlink_input_fences_fail_closed(tmp_path: Path) -> None:
    intent_value = _intent()
    facts_value = _facts(intent_value)
    source = tmp_path / "intent-source.json"
    source_digest = _write_document(source, intent_value)
    facts = tmp_path / "facts.json"
    facts_digest = _write_document(facts, facts_value)
    symlink = tmp_path / "intent-symlink.json"
    symlink.symlink_to(source)
    hardlink = tmp_path / "intent-hardlink.json"
    os.link(source, hardlink)
    for candidate in (symlink, hardlink):
        with pytest.raises(ProfileHostPlanError):
            plan_strict_cri_from_files(
                intent_path=str(candidate),
                expected_intent_digest=source_digest,
                facts_path=str(facts),
                expected_facts_digest=facts_digest,
            )


@pytest.mark.parametrize("mode", [0o644, 0o400, 0o660])
def test_input_mode_fence_fails_closed(tmp_path: Path, mode: int) -> None:
    intent_value = _intent()
    facts_value = _facts(intent_value)
    intent = tmp_path / "intent.json"
    facts = tmp_path / "facts.json"
    intent_digest = _write_document(intent, intent_value)
    facts_digest = _write_document(facts, facts_value)
    intent.chmod(mode)
    with pytest.raises(ProfileHostPlanError):
        plan_strict_cri_from_files(
            intent_path=str(intent),
            expected_intent_digest=intent_digest,
            facts_path=str(facts),
            expected_facts_digest=facts_digest,
        )


def test_digest_mismatch_and_noncanonical_input_path_fail_closed(tmp_path: Path) -> None:
    _intent_value, _facts_value, _plan, intent, facts = _from_files(tmp_path)
    with pytest.raises(ProfileHostPlanError):
        plan_strict_cri_from_files(
            intent_path=str(intent),
            expected_intent_digest="sha256:" + "0" * 64,
            facts_path=str(facts),
            expected_facts_digest=_digest(facts.read_bytes()),
        )
    with pytest.raises(ProfileHostPlanError):
        plan_strict_cri_from_files(
            intent_path=f"{tmp_path}/../{tmp_path.name}/intent.json",
            expected_intent_digest=_digest(intent.read_bytes()),
            facts_path=str(facts),
            expected_facts_digest=_digest(facts.read_bytes()),
        )


def test_oversized_input_fails_closed(tmp_path: Path) -> None:
    intent = tmp_path / "intent.json"
    intent.write_bytes(b" " * (host_plan.MAX_INPUT_BYTES + 1))
    intent.chmod(0o600)
    facts_value = _facts(_intent())
    facts = tmp_path / "facts.json"
    facts_digest = _write_document(facts, facts_value)
    with pytest.raises(ProfileHostPlanError):
        plan_strict_cri_from_files(
            intent_path=str(intent),
            expected_intent_digest=_digest(intent.read_bytes()),
            facts_path=str(facts),
            expected_facts_digest=facts_digest,
        )


def test_nonmutating_boundary_never_calls_write_or_process_primitives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    intent_value = _intent()
    facts_value = _facts(intent_value)
    intent = tmp_path / "intent.json"
    facts = tmp_path / "facts.json"
    intent_digest = _write_document(intent, intent_value)
    facts_digest = _write_document(facts, facts_value)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("mutation primitive called")

    for name in ("mkdir", "makedirs", "replace", "rename", "unlink", "remove", "chmod"):
        monkeypatch.setattr(host_plan.os, name, forbidden)
    plan = plan_strict_cri_from_files(
        intent_path=str(intent),
        expected_intent_digest=intent_digest,
        facts_path=str(facts),
        expected_facts_digest=facts_digest,
    )
    assert plan["mutating"] is False


def test_digest_collision_separation_changes_with_each_public_authority() -> None:
    intent = _intent()
    facts = _facts(intent)
    baseline = build_strict_cri_host_plan(
        intent,
        facts,
        intent_digest=_value_digest(intent),
        facts_digest=_value_digest(facts),
    )
    changed = copy.deepcopy(intent)
    changed["profileId"] = "core-b"
    candidate = build_strict_cri_host_plan(
        changed,
        facts,
        intent_digest=_value_digest(changed),
        facts_digest=_value_digest(facts),
    )
    assert candidate["planDigest"] != baseline["planDigest"]


def test_registry_insecure_and_system_trust_inputs_fail_closed() -> None:
    for field, value in (("scheme", "http"), ("systemTrust", True)):
        intent = _intent(registry=True)
        registry = intent["registry"]
        assert isinstance(registry, dict)
        registry[field] = value
        facts = _facts(intent)
        with pytest.raises(ProfileHostPlanError):
            build_strict_cri_host_plan(
                intent,
                facts,
                intent_digest=_value_digest(intent),
                facts_digest=_value_digest(facts),
            )
