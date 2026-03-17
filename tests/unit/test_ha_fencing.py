from pathlib import Path

from ae.ha.fencing import MutationEnvelope, SQLiteFenceStore, parse_envelope


def test_fence_store_accepts_first_operation(tmp_path: Path) -> None:
    store = SQLiteFenceStore(tmp_path / "fence.db")
    store.init()

    env = MutationEnvelope("ctrl-a", 7, "work:w1:1")
    decision = store.begin("site:sea", env)

    assert decision.accepted is True
    assert decision.epoch_advanced is True
    assert decision.current is not None
    assert decision.current.controller_id == "ctrl-a"
    assert decision.current.controller_epoch == 7


def test_fence_store_rejects_lower_epoch(tmp_path: Path) -> None:
    store = SQLiteFenceStore(tmp_path / "fence.db")
    store.init()
    store.commit("site:sea", MutationEnvelope("ctrl-a", 7, "work:w1:1"))

    decision = store.begin("site:sea", MutationEnvelope("ctrl-a", 6, "work:w2:1"))

    assert decision.stale is True
    assert decision.current is not None
    assert decision.current.controller_epoch == 7


def test_fence_store_accepts_higher_epoch_and_advances(tmp_path: Path) -> None:
    store = SQLiteFenceStore(tmp_path / "fence.db")
    store.init()
    store.commit("site:sea", MutationEnvelope("ctrl-a", 7, "work:w1:1"))

    decision = store.begin("site:sea", MutationEnvelope("ctrl-b", 8, "work:w2:1"))

    assert decision.accepted is True
    assert decision.epoch_advanced is True
    assert decision.current is not None
    assert decision.current.controller_id == "ctrl-b"
    assert decision.current.controller_epoch == 8


def test_fence_store_returns_duplicate_after_commit(tmp_path: Path) -> None:
    store = SQLiteFenceStore(tmp_path / "fence.db")
    store.init()
    env = MutationEnvelope("ctrl-a", 7, "work:w1:1")

    first = store.begin("site:sea", env)
    assert first.accepted is True
    store.commit("site:sea", env)

    duplicate = store.begin("site:sea", env)
    assert duplicate.duplicate is True


def test_parse_envelope_requires_all_fields() -> None:
    assert parse_envelope({}) is None
    env = parse_envelope(
        {
            "controller_id": "ctrl-a",
            "controller_epoch": 7,
            "operation_id": "work:w1:1",
        }
    )
    assert env == MutationEnvelope("ctrl-a", 7, "work:w1:1")
