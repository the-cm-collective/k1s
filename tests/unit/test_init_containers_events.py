from pathlib import Path

from ae.controller.reconciler import Reconciler
from ae.controller.spec import AppManifest, AppSpec, Metadata
from ae.controller.state import SQLiteStateStore


class DummyRuntimeWithInit:
    def __init__(self):
        self.calls = []

    def ensure_app(
        self,
        manifest,
        revision,
        *,
        keep_old=False,
        limit_create=None,
        pod_names: list[str] | None = None,
        node_id: str | None = None,
    ):  # noqa: ANN001
        from ae.runtime.base import PodState, RuntimeResult

        _ = (keep_old, limit_create, node_id)
        self.calls.append("ensure")
        name = (
            pod_names[0]
            if pod_names
            else f"{manifest.metadata.name}-rev{revision}-0"
        )
        return RuntimeResult(
            revision=revision,
            created=1,
            updated=0,
            removed=0,
            pod_states=[
                PodState(
                    pod_name=name,
                    ready=True,
                    status="running",
                    endpoint="127.0.0.1:9000",
                )
            ],
        )

    def run_init_containers(self, _manifest):  # noqa: ANN001
        return [("init", 0, "ok")]


def test_init_containers_emit_events(tmp_path: Path) -> None:
    store = SQLiteStateStore(tmp_path / "state.db")
    rt = DummyRuntimeWithInit()
    rec = Reconciler(rt, store)
    man = AppManifest(
        apiVersion="ae.dev/v1alpha1",
        kind="Deployment",
        metadata=Metadata(name="echo"),
        spec=AppSpec(
            image="alpine:3.20",
            initContainers=[{"name": "init", "image": "alpine:3.20", "command": ["/bin/true"]}],
        ),  # type: ignore[arg-type]
    )
    rec.reconcile(man)
    events = store.list_events("echo")
    codes = [e.event_type for e in events]
    assert any(t in codes for t in ("InitStart", "InitDone"))


# ruff: noqa: E501
