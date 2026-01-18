import socket
import threading

from ae.controller.reconciler import Reconciler
from ae.runtime.base import ReplicaState, RuntimeAdapter, RuntimeResult


class FakeRuntime(RuntimeAdapter):  # type: ignore[misc]
    def __init__(self, rc: int = 0):
        self._rc = rc

    def ensure_app(self, _manifest, revision, *, keep_old=False, limit_create=None):  # type: ignore[no-untyped-def]
        _ = (keep_old, limit_create)
        return RuntimeResult(revision=revision, created=0, updated=0, removed=0, replica_states=[])

    def read_logs(self, _replica_id, *, _follow=False, _tail=None, _since=None):  # type: ignore[no-untyped-def]
        return []

    def remove_app(self, _app_name: str) -> int:  # type: ignore[override]
        return 0

    def remove_old_revisions(self, _app_name: str, _keep_revision: int) -> int:  # type: ignore[override]
        return 0

    def ensure_storage_volumes(self, _app_name: str, _volumes: list[dict]) -> None:  # type: ignore[override]
        return None

    def remove_storage_volumes(self, _app_name: str, _names: list[str]) -> int:  # type: ignore[override]
        return 0

    def list_storage_volumes(self, _app_name: str | None = None) -> list[dict]:  # type: ignore[override]
        return []

    def list_containers_info(self) -> list[dict]:  # type: ignore[override]
        return []

    def exec(self, _replica_id: str, _command: list[str], *, timeout: int | None = None) -> int:  # type: ignore[override]
        _ = timeout
        return int(self._rc)


def _reconciler_with_rep(replica_ready=True, endpoint="127.0.0.1:9", rc=0):
    rt = FakeRuntime(rc=rc)
    from ae.config.manager import ConfigManager
    from ae.controller.health import HealthManager
    from ae.controller.state import SQLiteStateStore
    from ae.secrets import SecretManager

    store = SQLiteStateStore(":memory:")
    reconciler = Reconciler(
        rt,
        store,
        health_manager=HealthManager(),
        ingress_service=None,
        secret_manager=SecretManager(),
        config_manager=ConfigManager(),
    )
    # runtime_result with one replica
    rep = ReplicaState(
        replica_id="r1",
        ready=bool(replica_ready),
        status="running",
        endpoint=endpoint,
        started_at=None,
    )
    rr = RuntimeResult(revision=1, created=0, updated=0, removed=0, replica_states=[rep])

    # simple manifest stub
    class M:
        pass

    m = M()

    class Meta:
        pass

    m.metadata = Meta()
    m.metadata.name = "app"
    return reconciler, m, rr


def test_hook_exec_success():
    r, m, rr = _reconciler_with_rep(rc=0)
    ok, msg = r._run_rollout_hook(
        m, rr, {"name": "preSwitch", "exec": ["true"], "timeoutSeconds": 1}
    )
    assert ok is True


def test_hook_exec_failure():
    r, m, rr = _reconciler_with_rep(rc=2)
    ok, msg = r._run_rollout_hook(
        m, rr, {"name": "preSwitch", "exec": ["false"], "timeoutSeconds": 1}
    )
    assert ok is False


def _serve_once(port: int):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", port))
    s.listen(1)

    def _loop():
        try:
            conn, _ = s.accept()
            try:
                conn.recv(1)
            except Exception:
                pass
            try:
                conn.close()
            except Exception:
                pass
        finally:
            try:
                s.close()
            except Exception:
                pass

    th = threading.Thread(target=_loop, daemon=True)
    th.start()


def test_hook_tcp_success():
    port = 55667
    _serve_once(port)
    r, m, rr = _reconciler_with_rep(endpoint="127.0.0.1:1")
    ok, msg = r._run_rollout_hook(
        m, rr, {"name": "preSwitch", "tcp": {"port": port}, "timeoutSeconds": 1}
    )
    assert ok is True


def test_hook_tcp_failure():
    r, m, rr = _reconciler_with_rep(endpoint="127.0.0.1:1")
    ok, msg = r._run_rollout_hook(
        m, rr, {"name": "preSwitch", "tcp": {"port": 9}, "timeoutSeconds": 1}
    )
    assert ok is False


# ruff: noqa: SIM105,S110
