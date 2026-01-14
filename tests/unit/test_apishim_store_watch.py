from __future__ import annotations

import threading
import time

from ae.apishim.store import ObjectStore


def collect_events(store: ObjectStore, evs: list[tuple[str, str]], stop_after: int = 3) -> None:
    gen = store.watch("", "v1", "configmaps", "demo", heartbeat_seconds=1)
    try:
        for et, obj in gen:
            evs.append((et, obj.name))
            if len(evs) >= stop_after:
                break
    finally:
        gen.close()


def test_watch_add_modify_delete(tmp_path) -> None:
    store = ObjectStore(db_path=tmp_path / "shim.db")
    events: list[tuple[str, str]] = []

    # Seed namespace and start watcher
    store.upsert("", "v1", "namespaces", None, "demo", {"name": "demo"}, {})
    t = threading.Thread(target=collect_events, args=(store, events))
    t.daemon = True
    t.start()

    # Give the watcher time to emit initial ADDEDs (none yet for configmaps)
    time.sleep(0.05)

    # Create, modify, delete a ConfigMap in demo
    store.upsert(
        "",
        "v1",
        "configmaps",
        "demo",
        "cm1",
        {"name": "cm1", "namespace": "demo"},
        {"k": "v1"},
    )
    store.upsert(
        "",
        "v1",
        "configmaps",
        "demo",
        "cm1",
        {"name": "cm1", "namespace": "demo"},
        {"k": "v2"},
    )
    store.delete("", "v1", "configmaps", "demo", "cm1")

    t.join(timeout=2)
    # We expect ADDED, MODIFIED, DELETED for cm1
    assert ("ADDED", "cm1") in events
    assert ("MODIFIED", "cm1") in events
    assert ("DELETED", "cm1") in events
