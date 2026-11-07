from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import ae.kctl.profile_bootstrap as bootstrap_module
from ae.kctl.profile_backend import ComponentPlan, portable_plan_digest
from ae.kctl.profile_bootstrap import (
    ProfileBootstrapError,
    ProfileBootstrapStore,
    _private_digest,
)
from ae.kctl.profile_engine import EngineCapability
from ae.kctl.profile_paths import ProfileRoots

NOW = "2026-11-07T12:00:00Z"


def _roots(tmp_path: Path) -> ProfileRoots:
    return ProfileRoots.from_environment(
        {
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "XDG_STATE_HOME": str(tmp_path / "state"),
            "XDG_DATA_HOME": str(tmp_path / "data"),
            "XDG_CACHE_HOME": str(tmp_path / "cache"),
        },
        owner_uid=os.geteuid(),
    )


def _credential(tmp_path: Path, name: str, payload: str) -> Path:
    path = tmp_path / name
    path.write_text(payload, encoding="utf-8")
    path.chmod(0o600)
    return path


def _store(tmp_path: Path, profile: str, byte: int = 1) -> ProfileBootstrapStore:
    return ProfileBootstrapStore(
        _roots(tmp_path),
        profile,
        clock=lambda: NOW,
        random_bytes=lambda length: bytes([byte]) * length,
    )


def _node_record(tmp_path: Path, credential_text: str = "private-alpha"):
    store = _store(tmp_path, "node")
    record = store.create_node(
        node_id="node-a",
        controller_url="https://controller.example.test:9443",
        runtime_backend="podman",
        listen_host="127.0.0.1",
        listen_port=9109,
        advertise_endpoint="https://node-a.example.test:9109",
        credential_file=_credential(tmp_path, "node-credential", credential_text),
    )
    return store, record


def _capability() -> EngineCapability:
    return EngineCapability(
        backend="podman",
        executable=Path("/usr/bin/podman"),
        executable_device=1,
        executable_inode=2,
        version="5.0.0",
        rootless=True,
    )


def test_gateway_bootstrap_is_scoped_private_and_publicly_redacted(tmp_path: Path) -> None:
    store = _store(tmp_path, "edge-gateway")
    source = _credential(tmp_path, "gateway.creds", "-----BEGIN NATS USER JWT-----\nabc\n")

    record = store.create_edge_gateway(
        site_id="site-a",
        node_id="gateway-a",
        transport_url="tls://nats.example.test:4222",
        credential_file=source,
    )

    assert record.node_id == "site-a--gateway-a"
    assert record.backend_id == "gateway"
    assert record.path == store.layout.bootstrap_path(record.bootstrap_id)
    assert record.path.stat().st_mode & 0o777 == 0o600
    projection = record.public_projection()
    encoded = json.dumps(projection, sort_keys=True)
    assert projection["profile"] == "edge-gateway"
    assert projection["identityVersion"] == 1
    assert "credential" not in encoded.lower()
    assert str(source) not in encoded
    assert str(record.path) not in encoded
    assert record.credential not in repr(record)
    assert record.private_record_digest not in repr(record)


def test_bootstrap_public_projection_repr_digest_and_errors_hide_all_private_verifiers(
    tmp_path: Path,
) -> None:
    private_value = "private-value-that-must-never-render"
    store, record = _node_record(tmp_path, private_value)
    projection = json.dumps(record.public_projection(), sort_keys=True)
    public_digest = portable_plan_digest(
        "node",
        _capability(),
        bootstrap=record,
        python_executable=Path(sys.executable).resolve(strict=True),
    )
    direct_digest = hashlib.sha256(private_value.encode("utf-8")).hexdigest()
    rendered = "\n".join((projection, public_digest, repr(record)))
    private_plan = ComponentPlan(
        component_id="node-agent",
        dependencies=(),
        argv=("python", "-m", "ae.node.server"),
        child_environment=(("AE_AGENT_TOKEN", private_value),),
        cwd=tmp_path,
        log_path=tmp_path / "node-agent.log",
        readiness=(),
    )
    rendered += repr(private_plan)

    for forbidden in (
        private_value,
        direct_digest,
        record.private_record_digest,
        str(record.path),
        str(tmp_path / "node-credential"),
    ):
        assert forbidden not in rendered

    record.path.write_bytes(b'{"credential":"private-value-that-must-never-render"')
    record.path.chmod(0o600)
    with pytest.raises(ProfileBootstrapError) as captured:
        store.load(record.bootstrap_id)
    error_text = str(captured.value)
    assert private_value not in error_text
    assert direct_digest not in error_text
    assert str(record.path) not in error_text


def test_node_bootstrap_uses_explicit_identity_v1_and_exact_backend(tmp_path: Path) -> None:
    store, record = _node_record(tmp_path)

    loaded = store.load(record.bootstrap_id)
    assert loaded == record
    assert loaded.public_projection() == {
        "advertiseEndpoint": "https://node-a.example.test:9109",
        "backendId": "podman",
        "bootstrapId": record.bootstrap_id,
        "controllerUrl": "https://controller.example.test:9443",
        "identityVersion": 1,
        "listenHost": "127.0.0.1",
        "listenPort": 9109,
        "nodeId": "node-a",
        "profile": "node",
        "siteId": None,
        "transportUrl": None,
    }


def test_public_plan_digest_ignores_credential_bytes_but_private_revalidation_detects_drift(
    tmp_path: Path,
) -> None:
    store, original = _node_record(tmp_path, "private-alpha")
    executable = Path(sys.executable).resolve(strict=True)
    before = portable_plan_digest(
        "node",
        _capability(),
        bootstrap=original,
        python_executable=executable,
    )
    payload = json.loads(original.path.read_text(encoding="utf-8"))
    payload["credential"] = "private-bravo"
    payload["privateRecordDigest"] = _private_digest(payload)
    original.path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    original.path.chmod(0o600)

    changed = store.load(original.bootstrap_id)
    after = portable_plan_digest(
        "node",
        _capability(),
        bootstrap=changed,
        python_executable=executable,
    )

    assert before == after
    assert original.public_projection() == changed.public_projection()
    assert original.private_record_digest != changed.private_record_digest
    with pytest.raises(ProfileBootstrapError, match="identity changed"):
        store.revalidate(original)


def test_revalidation_rejects_same_bytes_at_a_replaced_inode(tmp_path: Path) -> None:
    store, record = _node_record(tmp_path)
    replacement = record.path.with_name("replacement.json")
    replacement.write_bytes(record.path.read_bytes())
    replacement.chmod(0o600)
    replacement.replace(record.path)

    with pytest.raises(ProfileBootstrapError, match="identity changed"):
        store.revalidate(record)


@pytest.mark.parametrize(
    ("profile", "kwargs"),
    [
        (
            "edge-gateway",
            {
                "site_id": "site-a",
                "node_id": "other--gateway-a",
                "transport_url": "tls://nats.example.test:4222",
            },
        ),
        (
            "node",
            {
                "node_id": "node-a",
                "controller_url": "https://user:secret@controller.example.test",
                "runtime_backend": "podman",
                "listen_host": "localhost",
                "listen_port": 9109,
                "advertise_endpoint": "https://node-a.example.test:9109",
            },
        ),
    ],
)
def test_bootstrap_invalid_identity_and_endpoint_inputs_fail_closed(
    tmp_path: Path,
    profile: str,
    kwargs: dict[str, object],
) -> None:
    store = _store(tmp_path, profile)
    source = _credential(tmp_path, f"{profile}.credential", "token-alpha")

    with pytest.raises((ProfileBootstrapError, ValueError)):
        if profile == "edge-gateway":
            store.create_edge_gateway(credential_file=source, **kwargs)  # type: ignore[arg-type]
        else:
            store.create_node(credential_file=source, **kwargs)  # type: ignore[arg-type]


def test_bootstrap_rejects_aliases_unsafe_credentials_and_random_collisions(
    tmp_path: Path,
) -> None:
    with pytest.raises(ProfileBootstrapError, match="limited to edge-gateway and node"):
        _store(tmp_path, "edge-core")

    unsafe = _credential(tmp_path, "unsafe-token", "token-alpha")
    unsafe.chmod(0o644)
    store = _store(tmp_path, "node")
    with pytest.raises(ProfileBootstrapError, match="ownership or mode"):
        store.create_node(
            node_id="node-a",
            controller_url="https://controller.example.test",
            runtime_backend="podman",
            listen_host="127.0.0.1",
            listen_port=9109,
            advertise_endpoint="https://node-a.example.test:9109",
            credential_file=unsafe,
        )

    first_source = _credential(tmp_path, "first-token", "token-alpha")
    store.create_node(
        node_id="node-a",
        controller_url="https://controller.example.test",
        runtime_backend="podman",
        listen_host="127.0.0.1",
        listen_port=9109,
        advertise_endpoint="https://node-a.example.test:9109",
        credential_file=first_source,
    )
    second_source = _credential(tmp_path, "second-token", "token-bravo")
    with pytest.raises(ProfileBootstrapError, match="unused bootstrap ID"):
        store.create_node(
            node_id="node-b",
            controller_url="https://controller.example.test",
            runtime_backend="podman",
            listen_host="127.0.0.1",
            listen_port=9110,
            advertise_endpoint="https://node-b.example.test:9110",
            credential_file=second_source,
        )


def test_concurrent_bootstrap_creates_serialize_and_publish_complete_records(
    tmp_path: Path,
) -> None:
    roots = _roots(tmp_path)
    first = ProfileBootstrapStore(
        roots,
        "node",
        clock=lambda: NOW,
        random_bytes=lambda length: bytes([10]) * length,
    )
    second = ProfileBootstrapStore(
        roots,
        "node",
        clock=lambda: NOW,
        random_bytes=lambda length: bytes([11]) * length,
    )
    with first.state_store.locked():
        pass
    barrier = threading.Barrier(2)

    def create(store: ProfileBootstrapStore, suffix: str, port: int):
        source = _credential(tmp_path, f"credential-{suffix}", f"private-{suffix}")
        barrier.wait(timeout=5)
        return store.create_node(
            node_id=f"node-{suffix}",
            controller_url="https://controller.example.test:9443",
            runtime_backend="podman",
            listen_host="127.0.0.1",
            listen_port=port,
            advertise_endpoint=f"https://node-{suffix}.example.test:{port}",
            credential_file=source,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = (
            pool.submit(create, first, "a", 9109),
            pool.submit(create, second, "b", 9110),
        )
        records = tuple(future.result(timeout=10) for future in futures)

    assert records[0].bootstrap_id != records[1].bootstrap_id
    assert {first.load(item.bootstrap_id).node_id for item in records} == {
        "node-a",
        "node-b",
    }
    members = sorted(first.layout.bootstraps.iterdir())
    assert members == sorted(item.path for item in records)
    for item in members:
        payload = item.read_bytes()
        assert payload.endswith(b"\n")
        decoded = json.loads(payload)
        assert payload == (
            json.dumps(decoded, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
    assert not any(".tmp-" in item.name for item in members)


def test_atomic_bootstrap_write_failure_publishes_neither_target_nor_temporary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path, "node")
    source = _credential(tmp_path, "node-credential", "private-alpha")

    def fail_write(fd: int, payload: bytes) -> None:
        os.write(fd, payload[:10])
        raise OSError("injected partial write")

    monkeypatch.setattr(bootstrap_module, "_write_all", fail_write)
    with pytest.raises(ProfileBootstrapError, match="storage is unavailable or unsafe"):
        store.create_node(
            node_id="node-a",
            controller_url="https://controller.example.test",
            runtime_backend="podman",
            listen_host="127.0.0.1",
            listen_port=9109,
            advertise_endpoint="https://node-a.example.test:9109",
            credential_file=source,
        )

    assert store.layout.bootstraps.is_dir()
    assert list(store.layout.bootstraps.iterdir()) == []


def test_bootstrap_reader_rejects_truncated_duplicate_unknown_and_noncanonical_json(
    tmp_path: Path,
) -> None:
    store, record = _node_record(tmp_path)
    original = record.path.read_bytes()
    decoded = json.loads(original)
    unknown = {**decoded, "unknown": True}
    duplicate = original.replace(
        b"{\n",
        b'{\n  "apiVersion": "k1s.dev/v1alpha1",\n',
        1,
    )
    variants = (
        original[:-1],
        original[:-5] + b"\x00bad",
        duplicate,
        (json.dumps(unknown, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(),
        json.dumps(decoded, sort_keys=True).encode(),
    )

    for payload in variants:
        record.path.write_bytes(payload)
        record.path.chmod(0o600)
        with pytest.raises(ProfileBootstrapError):
            store.load(record.bootstrap_id)
        record.path.write_bytes(original)
        record.path.chmod(0o600)


@pytest.mark.parametrize(
    ("name", "payload"),
    [
        ("nul", b"private\x00value"),
        ("invalid-utf8", b"private-\xff"),
        ("oversize", b"x" * (64 * 1024 + 1)),
    ],
)
def test_credential_content_failures_are_rejected(
    tmp_path: Path,
    name: str,
    payload: bytes,
) -> None:
    source = tmp_path / name
    source.write_bytes(payload)
    source.chmod(0o600)

    with pytest.raises(ProfileBootstrapError):
        _store(tmp_path, "node").create_node(
            node_id="node-a",
            controller_url="https://controller.example.test",
            runtime_backend="podman",
            listen_host="127.0.0.1",
            listen_port=9109,
            advertise_endpoint="https://node-a.example.test:9109",
            credential_file=source,
        )


def test_credential_symlink_and_hardlink_are_rejected(tmp_path: Path) -> None:
    source = _credential(tmp_path, "source", "private-alpha")
    symlink = tmp_path / "symlink"
    symlink.symlink_to(source)
    hardlink = tmp_path / "hardlink"
    os.link(source, hardlink)
    store = _store(tmp_path, "node")

    for candidate in (symlink, hardlink):
        with pytest.raises(ProfileBootstrapError):
            store.create_node(
                node_id="node-a",
                controller_url="https://controller.example.test",
                runtime_backend="podman",
                listen_host="127.0.0.1",
                listen_port=9109,
                advertise_endpoint="https://node-a.example.test:9109",
                credential_file=candidate,
            )


@pytest.mark.parametrize(
    "payload",
    (" private-alpha", "private-alpha ", "private alpha", "private-alpha\n"),
)
def test_node_token_noncanonical_spelling_is_rejected(
    tmp_path: Path,
    payload: str,
) -> None:
    source = _credential(tmp_path, "node-credential", payload)

    with pytest.raises(ProfileBootstrapError, match="one canonical line"):
        _store(tmp_path, "node").create_node(
            node_id="node-a",
            controller_url="https://controller.example.test",
            runtime_backend="podman",
            listen_host="127.0.0.1",
            listen_port=9109,
            advertise_endpoint="https://node-a.example.test:9109",
            credential_file=source,
        )


def test_wrong_role_invalid_id_and_unsafe_record_mode_fail_closed(tmp_path: Path) -> None:
    roots = _roots(tmp_path)
    gateway = ProfileBootstrapStore(
        roots,
        "edge-gateway",
        clock=lambda: NOW,
        random_bytes=lambda length: bytes([20]) * length,
    )
    gateway_record = gateway.create_edge_gateway(
        site_id="site-a",
        node_id="gateway-a",
        transport_url="tls://nats.example.test:4222",
        credential_file=_credential(tmp_path, "gateway-credential", "private-gateway"),
    )
    node = ProfileBootstrapStore(
        roots,
        "node",
        clock=lambda: NOW,
        random_bytes=lambda length: bytes([21]) * length,
    )

    with pytest.raises(ProfileBootstrapError, match="role changed"):
        node.revalidate(gateway_record)
    with pytest.raises(ProfileBootstrapError, match="bootstrap ID is invalid"):
        node.load("b1-invalid")

    gateway_record.path.chmod(0o644)
    with pytest.raises(ProfileBootstrapError, match="ownership or mode"):
        gateway.load(gateway_record.bootstrap_id)


def test_wrong_role_record_at_exact_foreign_role_path_is_rejected(tmp_path: Path) -> None:
    roots = _roots(tmp_path)
    gateway = ProfileBootstrapStore(
        roots,
        "edge-gateway",
        clock=lambda: NOW,
        random_bytes=lambda length: bytes([22]) * length,
    )
    record = gateway.create_edge_gateway(
        site_id="site-a",
        node_id="gateway-a",
        transport_url="tls://nats.example.test:4222",
        credential_file=_credential(tmp_path, "gateway-credential", "private-gateway"),
    )
    node = ProfileBootstrapStore(roots, "node", clock=lambda: NOW)
    with node.state_store.locked():
        node.layout.ensure_bootstrap_directory()
    wrong_path = node.layout.bootstrap_path(record.bootstrap_id)
    wrong_path.write_bytes(record.path.read_bytes())
    wrong_path.chmod(0o600)

    with pytest.raises(ProfileBootstrapError, match="identity differs from its owner"):
        node.load(record.bootstrap_id)


def test_unsafe_bootstrap_directory_and_final_target_are_never_repaired(
    tmp_path: Path,
) -> None:
    roots = _roots(tmp_path)
    store = ProfileBootstrapStore(
        roots,
        "node",
        clock=lambda: NOW,
        random_bytes=lambda length: bytes([23]) * length,
    )
    source = _credential(tmp_path, "node-credential", "private-alpha")
    with store.state_store.locked():
        store.layout.ensure_bootstrap_directory()
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    store.layout.bootstraps.rmdir()
    store.layout.bootstraps.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ProfileBootstrapError, match="storage is unavailable or unsafe"):
        store.create_node(
            node_id="node-a",
            controller_url="https://controller.example.test",
            runtime_backend="podman",
            listen_host="127.0.0.1",
            listen_port=9109,
            advertise_endpoint="https://node-a.example.test:9109",
            credential_file=source,
        )
    assert list(outside.iterdir()) == []


def test_temporary_and_final_collisions_are_fail_closed_without_overwrite(
    tmp_path: Path,
) -> None:
    roots = _roots(tmp_path)
    byte = 24
    store = ProfileBootstrapStore(
        roots,
        "node",
        clock=lambda: NOW,
        random_bytes=lambda length: bytes([byte]) * length,
    )
    source = _credential(tmp_path, "node-credential", "private-alpha")
    with store.state_store.locked():
        store.layout.ensure_bootstrap_directory()
    identifier = f"b1-{bytes([byte]).hex() * 16}"
    final_path = store.layout.bootstrap_path(identifier)
    final_path.write_text("owned collision", encoding="utf-8")
    final_path.chmod(0o600)

    with pytest.raises(ProfileBootstrapError, match="unused bootstrap ID"):
        store.create_node(
            node_id="node-a",
            controller_url="https://controller.example.test",
            runtime_backend="podman",
            listen_host="127.0.0.1",
            listen_port=9109,
            advertise_endpoint="https://node-a.example.test:9109",
            credential_file=source,
        )
    assert final_path.read_text(encoding="utf-8") == "owned collision"

    final_path.unlink()
    temporary = final_path.with_name(f".{final_path.name}.tmp-{bytes([byte]).hex() * 16}")
    temporary.write_text("temporary collision", encoding="utf-8")
    temporary.chmod(0o600)
    with pytest.raises(ProfileBootstrapError, match="private bootstrap temporary"):
        store.create_node(
            node_id="node-a",
            controller_url="https://controller.example.test",
            runtime_backend="podman",
            listen_host="127.0.0.1",
            listen_port=9109,
            advertise_endpoint="https://node-a.example.test:9109",
            credential_file=source,
        )
    assert temporary.read_text(encoding="utf-8") == "temporary collision"
    assert not final_path.exists()
