from ae.controller.spec import AppManifest, AppSpec, Metadata, SecuritySpec
from ae.runtime.podman_runtime import PodmanRuntime


def test_podman_security_flags_mapping(monkeypatch, tmp_path):
    # Prepare manifest with security context
    spec = AppSpec(
        image="alpine:3.20",
        security=SecuritySpec(runAsUser=1000, runAsGroup=1000, readOnlyRootFilesystem=True, dropCapabilities=["NET_RAW", "SYS_PTRACE"]),
    )
    manifest = AppManifest(apiVersion="ae.dev/v1alpha1", kind="App", metadata=Metadata(name="demo-sec"), spec=spec)

    runtime = PodmanRuntime()
    recorded = {"cmds": []}

    def fake_run_ok(argv, *, allow_fail=False):  # type: ignore[override]
        recorded["cmds"].append(argv)
        # Synthesize minimal responses for queries
        if argv[:3] == [runtime._bin, "ps", "-a"] and "--format" in argv:
            return type("R", (), {"code": 0, "out": "[]", "err": ""})()
        if argv[:2] == [runtime._bin, "pull"]:
            return type("R", (), {"code": 0, "out": "", "err": ""})()
        if argv[:2] == [runtime._bin, "run"]:
            return type("R", (), {"code": 0, "out": "", "err": ""})()
        if argv[:2] == [runtime._bin, "inspect"]:
            # After creation, ensure_app inspects final containers; return one container with labels
            data = [
                {
                    "Id": "abc123",
                    "Name": "ae-demo-sec-rev1-0",
                    "Config": {"Labels": {PodmanRuntime.APP_LABEL: "demo-sec", PodmanRuntime.REPLICA_LABEL: "demo-sec-rev1-0", PodmanRuntime.REVISION_LABEL: "1"}},
                    "State": {"Status": "running", "StartedAt": "2025-10-26T22:00:00Z"},
                    "NetworkSettings": {"Ports": {"8080/tcp": None}},
                }
            ]
            import json

            return type("R", (), {"code": 0, "out": json.dumps(data), "err": ""})()
        return type("R", (), {"code": 0, "out": "", "err": ""})()

    monkeypatch.setattr(runtime, "_run_ok", fake_run_ok)

    # Call private create directly to validate flags without full ensure flow
    runtime._create_container(manifest, "demo-sec-rev1-0", 1)  # type: ignore[attr-defined]

    # Find the run command issued
    run_cmds = [c for c in recorded["cmds"] if len(c) >= 3 and c[1] == "run"]
    assert run_cmds, "no podman run captured"
    run = run_cmds[-1]
    # Security flags present
    assert "--user" in run and ("1000:1000" in run or "--user" in run and run[run.index("--user") + 1] == "1000:1000")
    assert "--read-only" in run
    # Both caps dropped
    drops = [run[i + 1] for i, t in enumerate(run) if t == "--cap-drop" and (i + 1) < len(run)]
    assert set(drops) >= {"NET_RAW", "SYS_PTRACE"}

