import sys
import types

# Avoid importing grpc-heavy runtimes during unit test collection.
sys.modules.setdefault("grpc", types.SimpleNamespace())

from ae.apishim.adapter import build_adapter
from ae.apishim.store import ObjectStore
from ae.controller.state import SQLiteStateStore
from ae.runtime.docker_stub import StubRuntime


def test_apishim_statefulset_creates_claims_per_ordinal(tmp_path):
    store = ObjectStore(db_path=tmp_path / "apishim.db")
    state = SQLiteStateStore(tmp_path / "state.db")
    adapter = build_adapter(store, runtime=StubRuntime(), state_store=state)

    sts_spec = {
        "replicas": 2,
        "volumeClaimTemplates": [
            {
                "metadata": {"name": "data"},
                "spec": {
                    "accessModes": ["ReadWriteOnce"],
                    "resources": {"requests": {"storage": "1Gi"}},
                },
            }
        ],
    }
    store.upsert(
        "apps",
        "v1",
        "statefulsets",
        "default",
        "db",
        {"name": "db", "namespace": "default", "uid": "sts-uid"},
        sts_spec,
        status={},
    )
    sts = store.get("apps", "v1", "statefulsets", "default", "db")
    assert sts is not None

    adapter._ensure_statefulset_claims(sts)

    pvc0 = store.get("", "v1", "persistentvolumeclaims", "default", "data-db-0")
    pvc1 = store.get("", "v1", "persistentvolumeclaims", "default", "data-db-1")
    assert pvc0 is not None
    assert pvc1 is not None
    for pvc in (pvc0, pvc1):
        owner_refs = (pvc.metadata or {}).get("ownerReferences") or []
        assert owner_refs
        owner = owner_refs[0]
        assert owner.get("kind") == "StatefulSet"
        assert owner.get("name") == "db"
