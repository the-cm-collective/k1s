import pytest
from pydantic import ValidationError

from ae.controller.spec import AppManifest, AppSpec, Metadata, PodSecuritySpec, SecuritySpec
from ae.runtime.podman_runtime import PodmanRuntime


def test_podman_security_flags_mapping(tmp_path, monkeypatch):
    _ = tmp_path
    # Prepare manifest with security context
    spec = AppSpec(
        image="alpine:3.20",
        security=SecuritySpec(
            runAsUser=1000,
            runAsGroup=1000,
            readOnlyRootFilesystem=True,
            dropCapabilities=["NET_RAW", "SYS_PTRACE"],
        ),
    )
    manifest = AppManifest(
        apiVersion="ae.dev/v1alpha1",
        kind="Deployment",
        metadata=Metadata(name="demo-sec"),
        spec=spec,
    )

    runtime = PodmanRuntime()
    recorded = {"cmds": []}

    def fake_run_ok(argv, *, allow_fail=False):  # type: ignore[override]
        _ = allow_fail
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
                    "Config": {
                        "Labels": {
                            PodmanRuntime.APP_LABEL: "demo-sec",
                            PodmanRuntime.REPLICA_LABEL: "demo-sec-rev1-0",
                            PodmanRuntime.REVISION_LABEL: "1",
                        }
                    },
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
    assert "--user" in run and (
        "1000:1000" in run or "--user" in run and run[run.index("--user") + 1] == "1000:1000"
    )
    assert "--read-only" in run
    # Both caps dropped
    drops = [run[i + 1] for i, t in enumerate(run) if t == "--cap-drop" and (i + 1) < len(run)]
    assert set(drops) >= {"NET_RAW", "SYS_PTRACE"}


def test_security_spec_accepts_complete_cri_contract() -> None:
    security = SecuritySpec.model_validate(
        {
            "runAsUser": 1000,
            "runAsGroup": 2000,
            "runAsNonRoot": True,
            "privileged": False,
            "allowPrivilegeEscalation": False,
            "readOnlyRootFilesystem": True,
            "addCapabilities": ["NET_BIND_SERVICE"],
            "dropCapabilities": ["NET_RAW"],
            "maskedPaths": ["/proc/kcore"],
            "readOnlyPaths": ["/proc/sys"],
            "seccompProfileType": "Localhost",
            "seccompLocalhostProfile": "profiles/k1s.json",
            "apparmorProfile": "localhost/k1s-profile",
            "seLinuxUser": "system_u",
            "seLinuxRole": "system_r",
            "seLinuxType": "container_t",
            "seLinuxLevel": "s0:c1,c2",
        }
    )

    assert security.run_as_user == 1000
    assert security.run_as_group == 2000
    assert security.run_as_non_root is True
    assert security.allow_privilege_escalation is False
    assert security.add_caps == ["NET_BIND_SERVICE"]
    assert security.masked_paths == ["/proc/kcore"]
    assert security.seccomp_localhost_profile == "profiles/k1s.json"
    assert security.selinux_type == "container_t"


def test_pod_security_spec_accepts_supplemental_groups_and_exact_profiles() -> None:
    security = PodSecuritySpec.model_validate(
        {
            "fsGroup": 3000,
            "supplementalGroups": [1000, 2000, 1000],
            "seccompProfileType": "Localhost",
            "seccompLocalhostProfile": "profiles/pod.json",
            "seLinuxType": "container_t",
        }
    )

    assert security.fs_group == 3000
    assert security.supplemental_groups == [1000, 2000, 1000]
    assert security.seccomp_localhost_profile == "profiles/pod.json"


@pytest.mark.parametrize("field", ["runAsUser", "runAsGroup"])
@pytest.mark.parametrize("value", [True, "1000", -1, 1 << 63])
def test_security_spec_rejects_invalid_numeric_ids(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        SecuritySpec.model_validate({field: value})


@pytest.mark.parametrize("field", ["fsGroup", "supplementalGroups"])
@pytest.mark.parametrize("value", [True, "1000", -1, 1 << 63])
def test_pod_security_spec_rejects_invalid_group_ids(field: str, value: object) -> None:
    payload = {field: [value]} if field == "supplementalGroups" else {field: value}
    with pytest.raises(ValidationError):
        PodSecuritySpec.model_validate(payload)


@pytest.mark.parametrize(
    "payload",
    [
        {"runAsNonRoot": True},
        {"runAsNonRoot": True, "runAsUser": 0},
        {"runAsNonRoot": "true", "runAsUser": 1000},
        {"privileged": True, "allowPrivilegeEscalation": False},
        {"privileged": 1},
        {"allowPrivilegeEscalation": 0},
    ],
)
def test_security_spec_rejects_invalid_non_root_and_privilege_combinations(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        SecuritySpec.model_validate(payload)


@pytest.mark.parametrize(
    "model,payload",
    [
        (SecuritySpec, {"runAsUser": 1000, "run_as_user": 1000}),
        (SecuritySpec, {"unknownSecurityField": True}),
        (PodSecuritySpec, {"fsGroup": 1000, "fs_group": 1000}),
        (PodSecuritySpec, {"unknownPodSecurityField": True}),
    ],
)
def test_security_models_reject_duplicate_spellings_and_unknown_keys(
    model: type[SecuritySpec] | type[PodSecuritySpec],
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        model.model_validate(payload)


@pytest.mark.parametrize(
    "model,payload",
    [
        (SecuritySpec, {"addCapabilities": None}),
        (SecuritySpec, {"dropCapabilities": None}),
        (SecuritySpec, {"maskedPaths": None}),
        (SecuritySpec, {"readOnlyPaths": None}),
        (SecuritySpec, {"readOnlyRootFilesystem": None}),
        (PodSecuritySpec, {"supplementalGroups": None}),
    ],
)
def test_security_models_reject_null_list_and_required_bool_fields(
    model: type[SecuritySpec] | type[PodSecuritySpec],
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        model.model_validate(payload)


@pytest.mark.parametrize(
    "payload",
    [
        {"seccompProfileType": "Localhost"},
        {
            "seccompProfileType": "RuntimeDefault",
            "seccompLocalhostProfile": "profiles/k1s.json",
        },
        {
            "seccompProfileType": "Localhost",
            "seccompLocalhostProfile": "/profiles/k1s.json",
        },
        {
            "seccompProfileType": "Localhost",
            "seccompLocalhostProfile": "profiles/../k1s.json",
        },
        {
            "seccompProfileType": "Localhost",
            "seccompLocalhostProfile": "profiles//k1s.json",
        },
    ],
)
def test_security_spec_rejects_invalid_seccomp_pairings_and_paths(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        SecuritySpec.model_validate(payload)


@pytest.mark.parametrize(
    "field,value",
    [
        ("maskedPaths", ["proc/kcore"]),
        ("maskedPaths", ["/proc//kcore"]),
        ("maskedPaths", ["/proc/../kcore"]),
        ("readOnlyPaths", ["/proc/sys/"]),
        ("readOnlyPaths", ["/proc\\sys"]),
    ],
)
def test_security_spec_rejects_noncanonical_cri_paths(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        SecuritySpec.model_validate({field: value})


@pytest.mark.parametrize(
    "payload",
    [
        {"addCapabilities": [""]},
        {"dropCapabilities": [" NET_RAW"]},
        {"apparmorProfile": "localhost/"},
        {"apparmorProfile": " localhost/k1s"},
        {"seLinuxType": " "},
        {"seLinuxLevel": "s0\x00bad"},
    ],
)
def test_security_spec_rejects_invalid_exact_text(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        SecuritySpec.model_validate(payload)
