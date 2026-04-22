import socket
import subprocess

from ae.runtime.cri_runtime import CRIRuntime


class _FakeProc:
    def __init__(self, *, stdin=None, stdout=None, stderr=None, returncode: int = 0) -> None:
        self.stdin = stdin
        self.stdout = stdout
        self.stderr = stderr
        self._returncode = returncode

    def wait(self, timeout=None) -> int:
        _ = timeout
        return self._returncode

    def poll(self) -> int:
        return self._returncode


class _NoopThread:
    def __init__(self, *, target=None, args=(), kwargs=None, daemon=None) -> None:
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}
        self.daemon = daemon

    def start(self) -> None:
        return

    def join(self, timeout=None) -> None:
        _ = timeout
        return


def _runtime(monkeypatch) -> CRIRuntime:
    runtime = CRIRuntime(endpoint="unix:///run/containerd/containerd.sock")
    monkeypatch.setattr(runtime, "_ensure_clients", lambda: None)

    def _container_id_for_replica(_pod_name: str, *, container_label: str = "main") -> str:
        _ = container_label
        return "cid123"

    monkeypatch.setattr(runtime, "_container_id_for_replica", _container_id_for_replica)
    return runtime


def test_exec_attach_tty_uses_pty(monkeypatch):
    runtime = _runtime(monkeypatch)
    calls: list[tuple[list[str], dict, _FakeProc]] = []
    closed: list[int] = []
    resolved_crictl = "/usr/bin/crictl"

    def _fake_popen(argv, **kwargs):
        proc = _FakeProc(
            stdin=kwargs.get("stdin"),
            stdout=kwargs.get("stdout"),
            stderr=kwargs.get("stderr"),
        )
        calls.append((list(argv), kwargs, proc))
        return proc

    monkeypatch.setenv("CRICTL_BIN", resolved_crictl)
    monkeypatch.setattr("ae.runtime.cri_runtime.shutil.which", lambda _bin: resolved_crictl)
    monkeypatch.setattr("ae.runtime.cri_runtime.subprocess.Popen", _fake_popen)
    monkeypatch.setattr("ae.runtime.cri_runtime.threading.Thread", _NoopThread)
    monkeypatch.setattr("pty.openpty", lambda: (101, 102))
    monkeypatch.setattr("ae.runtime.cri_runtime.os.set_blocking", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("ae.runtime.cri_runtime.os.close", lambda fd: closed.append(fd))
    monkeypatch.setattr("ae.runtime.cri_runtime.os.read", lambda _fd, _n: b"")
    monkeypatch.setattr("ae.runtime.cri_runtime.os.write", lambda _fd, data: len(data))

    exec_sock, exec_id = runtime.exec_attach("pod-a", ["/bin/sh"], tty=True)

    argv, kwargs, _proc = calls[0]
    assert argv[0:5] == [
        resolved_crictl,
        "--runtime-endpoint",
        "unix:///run/containerd/containerd.sock",
        "exec",
        "-i",
    ]
    assert "-t" in argv
    assert argv[-2:] == ["cid123", "/bin/sh"]
    assert kwargs["stdin"] == 102
    assert kwargs["stdout"] == 102
    assert kwargs["stderr"] == 102
    assert kwargs["close_fds"] is True

    exec_sock.settimeout(0.1)
    exec_sock.sendall(b"echo hi\n")
    assert exec_sock.recv(1024) == b""
    exec_sock.close()

    assert 102 in closed
    assert 101 in closed
    assert runtime.exec_exit_code(exec_id) == 0


def test_exec_attach_non_tty_uses_pipes(monkeypatch):
    runtime = _runtime(monkeypatch)
    calls: list[tuple[list[str], dict, _FakeProc]] = []
    resolved_crictl = "/usr/bin/crictl"

    def _fake_popen(argv, **kwargs):
        proc = _FakeProc(
            stdin=kwargs.get("stdin"),
            stdout=kwargs.get("stdout"),
            stderr=kwargs.get("stderr"),
        )
        calls.append((list(argv), kwargs, proc))
        return proc

    monkeypatch.setenv("CRICTL_BIN", resolved_crictl)
    monkeypatch.setattr("ae.runtime.cri_runtime.shutil.which", lambda _bin: resolved_crictl)
    monkeypatch.setattr("ae.runtime.cri_runtime.subprocess.Popen", _fake_popen)
    monkeypatch.setattr("ae.runtime.cri_runtime.threading.Thread", _NoopThread)

    exec_sock, exec_id = runtime.exec_attach("pod-a", ["/bin/sh"], tty=False)
    assert isinstance(exec_sock, socket.socket)
    exec_sock.close()

    argv, kwargs, _proc = calls[0]
    assert argv[0] == resolved_crictl
    assert "-t" not in argv
    assert kwargs["stdin"] is subprocess.PIPE
    assert kwargs["stdout"] is subprocess.PIPE
    assert kwargs["stderr"] is subprocess.PIPE
    assert runtime.exec_exit_code(exec_id) == 0
