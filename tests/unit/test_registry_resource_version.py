from __future__ import annotations

from ae.controller.spec import AppManifest, AppSpec, Metadata
from ae.controller.state import RegistryConflictError, SQLiteStateStore


def _manifest(name: str, *, image: str = "alpine:3.20") -> AppManifest:
    return AppManifest(
        apiVersion="ae.dev/v1alpha1",
        kind="Deployment",
        metadata=Metadata(name=name),
        spec=AppSpec(image=image, replicas=1),
    )


def test_register_app_tracks_resource_version_and_rejects_stale_write(tmp_path) -> None:
    store = SQLiteStateStore(tmp_path / "state.db")

    rv1 = store.register_app(_manifest("echo"), source="test", labels={"env": "dev"})
    entry = store.get_registered_entry("echo")
    assert entry is not None
    assert rv1 == 1
    assert entry.resource_version == 1

    rv2 = store.register_app(
        _manifest("echo", image="alpine:3.21"),
        source="test",
        labels={"env": "dev"},
        expected_resource_version=entry.resource_version,
    )
    updated = store.get_registered_entry("echo")
    assert updated is not None
    assert rv2 == 2
    assert updated.resource_version == 2

    try:
        store.register_app(
            _manifest("echo", image="alpine:3.22"),
            source="test",
            labels={"env": "dev"},
            expected_resource_version=1,
        )
    except RegistryConflictError as exc:
        assert exc.expected == 1
        assert exc.actual == 2
    else:  # pragma: no cover - defensive
        raise AssertionError("expected RegistryConflictError")


def test_delete_registered_app_rejects_stale_resource_version(tmp_path) -> None:
    store = SQLiteStateStore(tmp_path / "state.db")
    store.register_app(_manifest("echo"), source="test", labels={})
    current = store.get_registered_entry("echo")
    assert current is not None

    store.register_app(
        _manifest("echo", image="alpine:3.21"),
        source="test",
        labels={},
        expected_resource_version=current.resource_version,
    )

    try:
        store.delete_registered_app("echo", expected_resource_version=current.resource_version)
    except RegistryConflictError as exc:
        assert exc.expected == 1
        assert exc.actual == 2
    else:  # pragma: no cover - defensive
        raise AssertionError("expected RegistryConflictError")
