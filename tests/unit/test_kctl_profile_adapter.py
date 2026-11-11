from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

import ae.kctl.profile_adapter as profile_adapter_module
from ae.kctl.profile_adapter import (
    ADAPTER_API_VERSION,
    EVIDENCE_API_VERSION,
    NODE_OBSERVATION_API_VERSION,
    ProfileAdapterError,
    adapt_workerbee_profile_evidence,
    load_workerbee_adapter_contract,
    render_workerbee_profile_evidence_json,
    verify_workerbee_profile_evidence,
)
from ae.kctl.profiles import (
    InstalledProfileFacts,
    profile_catalog_payload,
    profile_doctor_payload,
    profile_plan_payload,
)


class _StaticProbe:
    def inspect(self) -> InstalledProfileFacts:
        return InstalledProfileFacts(
            python_version=(3, 11),
            platform="linux",
            ae_importable=True,
            distribution_version="1.0.0",
            k1s_console_present=True,
        )


def _doctor(name: str = "core") -> tuple[dict[str, object], int]:
    return profile_doctor_payload(name, _StaticProbe())


def _status(*, status: str = "running", phase: str = "running") -> dict[str, object]:
    return {
        "action": "status",
        "backend": "podman",
        "exit_code": 0,
        "generation": 7,
        "phase": phase,
        "profile": "node",
        "qualification_posture": "source-validated/unqualified",
        "run_id": "r1-0123456789abcdef0123456789abcdef",
        "status": status,
    }


def _logs(lines: list[str] | None = None) -> dict[str, object]:
    return {
        "apiVersion": "k1s.dev/v1alpha1",
        "component": "node-agent",
        "kind": "ProfileLogSnapshot",
        "lines": ["private value", "token=must-not-escape"] if lines is None else lines,
        "profile": "node",
        "runId": "r1-0123456789abcdef0123456789abcdef",
        "truncated": False,
    }


def _bootstrap(*, endpoint: str = "https://controller.example:9443") -> dict[str, object]:
    return {
        "advertiseEndpoint": "https://node.example:9109",
        "backendId": "podman",
        "bootstrapId": "b1-0123456789abcdef0123456789abcdef",
        "controllerUrl": endpoint,
        "identityVersion": 1,
        "listenHost": "127.0.0.1",
        "listenPort": 9109,
        "nodeId": "node-a",
        "profile": "node",
        "siteId": None,
        "transportUrl": None,
    }


def _gateway() -> dict[str, object]:
    return {
        "advertiseEndpoint": None,
        "backendId": "gateway",
        "bootstrapId": "b1-fedcba9876543210fedcba9876543210",
        "controllerUrl": None,
        "identityVersion": 1,
        "listenHost": None,
        "listenPort": None,
        "nodeId": "site-a--gateway-a",
        "profile": "edge-gateway",
        "siteId": "site-a",
        "transportUrl": "tls://gateway.example:4222",
    }


def _observation(*, node_id: str = "node-a", generation: object = 11) -> dict[str, object]:
    return {
        "apiVersion": NODE_OBSERVATION_API_VERSION,
        "generation": generation,
        "identityMode": "explicit",
        "identityVersion": 1,
        "kind": "NodeLeaseObservation",
        "nodeId": node_id,
        "provenance": "k1s-controller-lease",
        "schemaVersion": 1,
    }


def _adapt(
    operation: str,
    source: dict[str, object],
    *,
    exit_code: int = 0,
    observation: dict[str, object] | None = None,
) -> dict[str, object]:
    return adapt_workerbee_profile_evidence(
        operation,
        source,
        source_exit_code=exit_code,
        node_observation=observation,
    )


def _assert_failed(code: str, function, *args, **kwargs) -> None:
    with pytest.raises(ProfileAdapterError) as raised:
        function(*args, **kwargs)
    assert raised.value.code == code
    assert str(raised.value) == "workerbee profile adapter failed closed"
    assert "private" not in repr(raised.value)


def _resign(evidence: dict[str, object]) -> dict[str, object]:
    unsigned = {key: value for key, value in evidence.items() if key != "evidenceId"}
    raw = (
        json.dumps(
            unsigned, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
        )
        + "\n"
    ).encode("utf-8")
    evidence["evidenceId"] = (
        "sha256:" + hashlib.sha256(b"k1s-workerbee-profile-evidence-v1\0" + raw).hexdigest()
    )
    return evidence


def test_contract_resource_is_exact_ordered_and_immutable() -> None:
    contract = load_workerbee_adapter_contract()
    assert [operation.name for operation in contract.operations] == [
        "list",
        "doctor",
        "plan",
        "status",
        "logs",
        "bootstrap",
    ]
    assert contract.descriptor("doctor").accepted_exit_codes == (0, 1)
    assert contract.descriptor("bootstrap").source_mutating is True
    assert contract.descriptor("bootstrap").node_observation == "optional"
    with pytest.raises(FrozenInstanceError):
        contract.operations[0].source_kind = "changed"  # type: ignore[misc]


def test_wp01_same_runtime_consumer_fixture_covers_all_six_operations() -> None:
    class SameRuntimeProvider:
        def __init__(self) -> None:
            self.acquired: list[str] = []

        def acquire(
            self, operation: str
        ) -> tuple[dict[str, object], int, dict[str, object] | None]:
            if operation == "bootstrap":
                raise AssertionError("bootstrap must be supplied as a selected projection")
            self.acquired.append(operation)
            if operation == "list":
                return profile_catalog_payload(), 0, None
            if operation == "doctor":
                source, exit_code = _doctor("core")
                return source, exit_code, None
            if operation == "plan":
                return profile_plan_payload("node"), 0, None
            if operation == "status":
                return _status(), 0, None
            if operation == "logs":
                return _logs(), 0, None
            raise AssertionError(f"unexpected operation: {operation}")

        def project(
            self,
            operation: str,
            *,
            selected_bootstrap: dict[str, object] | None = None,
        ) -> tuple[dict[str, object], str]:
            if operation == "bootstrap":
                if selected_bootstrap is None:
                    raise AssertionError("bootstrap projection must already be selected")
                source, exit_code, observation = selected_bootstrap, 0, _observation()
            else:
                if selected_bootstrap is not None:
                    raise AssertionError("selected bootstrap is operation-specific")
                source, exit_code, observation = self.acquire(operation)
            evidence = adapt_workerbee_profile_evidence(
                operation,
                source,
                source_exit_code=exit_code,
                node_observation=observation,
            )
            rendered = render_workerbee_profile_evidence_json(evidence)
            assert verify_workerbee_profile_evidence(rendered) == evidence
            return evidence, rendered

    provider = SameRuntimeProvider()
    contract = load_workerbee_adapter_contract()
    evidence_by_operation: dict[str, dict[str, object]] = {}
    canonical_by_operation: dict[str, str] = {}
    for descriptor in contract.operations:
        evidence, rendered = provider.project(
            descriptor.name,
            selected_bootstrap=_bootstrap() if descriptor.name == "bootstrap" else None,
        )
        evidence_by_operation[descriptor.name] = evidence
        canonical_by_operation[descriptor.name] = rendered

    assert provider.acquired == ["list", "doctor", "plan", "status", "logs"]
    assert list(evidence_by_operation) == [
        "list",
        "doctor",
        "plan",
        "status",
        "logs",
        "bootstrap",
    ]
    assert evidence_by_operation["logs"]["payload"] == {
        "component": "node-agent",
        "contentPolicy": "omitted",
        "lineCount": 2,
        "profile": "node",
        "runRef": "sha256:068b3d1b8fde45ba6ad811be983a07a60f15ad3e539851dbc3851ccb1f35a7ce",
        "sourceTruncated": False,
    }
    assert evidence_by_operation["bootstrap"]["nodeIdentity"] == {
        "generation": 11,
        "generationProvenance": "k1s-controller-lease",
        "identityMode": "explicit",
        "identityVersion": 1,
        "nodeId": "node-a",
    }

    retained_w4 = []
    for operation, evidence in evidence_by_operation.items():
        canonical = canonical_by_operation[operation]
        retained_w4.append(
            {
                "canonicalEvidence": canonical,
                "evidenceId": evidence["evidenceId"],
                "evidenceSha256": "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
                "operation": operation,
                "provenance": evidence["provenance"],
            }
        )
    assert all(
        set(item)
        == {
            "canonicalEvidence",
            "evidenceId",
            "evidenceSha256",
            "operation",
            "provenance",
        }
        for item in retained_w4
    )
    contract_raw = (
        Path(__file__).resolve().parents[2] / "src/ae/resources/profiles/workerbee-adapter-v1.json"
    ).read_bytes()
    retained_w4_set = {
        "adapterContract": contract_raw.decode("utf-8"),
        "adapterContractSha256": "sha256:" + hashlib.sha256(contract_raw).hexdigest(),
        "evidence": retained_w4,
    }
    assert {
        item["provenance"]["distributionVersion"]  # type: ignore[index]
        for item in retained_w4
    } == {"1.0.0"}
    retained_text = json.dumps(retained_w4_set, ensure_ascii=False, sort_keys=True)
    for forbidden in (
        "private value",
        "must-not-escape",
        "controller.example",
        "node.example",
        "127.0.0.1",
        "9109",
        "r1-0123456789abcdef0123456789abcdef",
        "credential",
        "privateBootstrapDigest",
    ):
        assert forbidden not in retained_text


@pytest.mark.parametrize(
    ("target", "replacement"),
    [
        ("apiVersion", "k1s.dev/workerbee-profile-adapter/v2"),
        ("evidenceApiVersion", "k1s.dev/workerbee-profile-evidence/v2"),
        ("kind", "Unknown"),
        ("qualificationPosture", "live-qualified"),
        ("schemaVersion", True),
        ("acceptedExitCodes", [1, 0]),
        ("adapterMutating", True),
        ("nodeObservation", "required"),
        ("sourceApiVersion", "k1s.dev/v2"),
        ("sourceKind", "Unknown"),
        ("sourceMode", "unknown"),
        ("sourceMutating", 0),
        ("sourceSchemaVersion", True),
    ],
)
def test_contract_resource_rejects_every_wrong_descriptor_literal(
    monkeypatch: pytest.MonkeyPatch, target: str, replacement: object
) -> None:
    resource = json.loads(
        (
            Path(__file__).resolve().parents[2]
            / "src/ae/resources/profiles/workerbee-adapter-v1.json"
        ).read_text(encoding="utf-8")
    )
    if target in resource:
        resource[target] = replacement
    else:
        resource["operations"][1][target] = replacement
    raw = (json.dumps(resource, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    profile_adapter_module.load_workerbee_adapter_contract.cache_clear()
    monkeypatch.setattr(profile_adapter_module.resource_loader, "load_binary", lambda *_parts: raw)
    _assert_failed("unsupported-schema", load_workerbee_adapter_contract)
    profile_adapter_module.load_workerbee_adapter_contract.cache_clear()


@pytest.mark.parametrize("operation", ["list", "doctor", "plan", "status", "logs"])
def test_node_observation_is_forbidden_without_bindable_bootstrap(operation: str) -> None:
    source: dict[str, object]
    exit_code = 0
    if operation == "list":
        source = profile_catalog_payload()
    elif operation == "doctor":
        source, exit_code = _doctor()
    elif operation == "plan":
        source = profile_plan_payload("node")
    elif operation == "status":
        source = _status()
    else:
        source = _logs()
    _assert_failed(
        "node-observation-mismatch",
        _adapt,
        operation,
        source,
        exit_code=exit_code,
        observation=_observation(),
    )


def test_list_projection_is_exact_and_omits_free_text() -> None:
    evidence = _adapt("list", profile_catalog_payload())
    assert evidence["apiVersion"] == EVIDENCE_API_VERSION
    assert evidence["nodeIdentity"] is None
    profiles = evidence["payload"]["profiles"]  # type: ignore[index]
    assert [profile["name"] for profile in profiles] == [  # type: ignore[index]
        "local",
        "core",
        "edge-gateway",
        "node",
        "edge-dev",
        "core-ha",
    ]
    assert all("summary" not in profile for profile in profiles)  # type: ignore[union-attr]
    assert all(
        "warning" not in alias
        for profile in profiles  # type: ignore[union-attr]
        for alias in profile["aliases"]
    )


@pytest.mark.parametrize(
    "name",
    [
        "local",
        "core",
        "edge-gateway",
        "node",
        "edge-dev",
        "edge",
        "core-edge",
        "edge-core",
        "core-ha",
    ],
)
def test_plan_warning_mapping_is_complete_and_raw_text_is_absent(name: str) -> None:
    evidence = _adapt("plan", profile_plan_payload(name))
    payload = evidence["payload"]
    assert "warnings" not in payload
    assert 1 <= len(payload["warningCodes"]) <= 3  # type: ignore[arg-type,index]
    assert all(isinstance(code, str) and code == code.strip() for code in payload["warningCodes"])  # type: ignore[index]


def test_plan_warning_mapping_rejects_unmapped_or_duplicate_text() -> None:
    source = profile_plan_payload("node")
    source["warnings"] = ["unknown warning"]
    _assert_failed("source-shape-mismatch", _adapt, "plan", source)

    source = profile_plan_payload("node")
    source["warnings"] = [source["warnings"][0], source["warnings"][0]]  # type: ignore[index]
    _assert_failed("source-shape-mismatch", _adapt, "plan", source)


def test_doctor_exit_and_status_mapping_are_exact() -> None:
    source, exit_code = _doctor("core")
    evidence = _adapt("doctor", source, exit_code=exit_code)
    assert evidence["payload"]["status"] == source["overallStatus"]  # type: ignore[index]
    assert all("summary" not in check for check in evidence["payload"]["checks"])  # type: ignore[index,union-attr]

    failed = copy.deepcopy(source)
    failed["checks"][0]["status"] = "fail"  # type: ignore[index]
    failed["overallStatus"] = "fail"
    assert _adapt("doctor", failed, exit_code=1)["payload"]["status"] == "fail"  # type: ignore[index]
    _assert_failed("source-exit-mismatch", _adapt, "doctor", failed, exit_code=0)


@pytest.mark.parametrize(
    ("status", "exit_code", "wrong_exit_code"),
    [("pass", 0, 1), ("warn", 0, 1), ("fail", 1, 0)],
)
def test_doctor_verifier_binds_recomputed_status_to_source_exit_code(
    status: str, exit_code: int, wrong_exit_code: int
) -> None:
    source, _ = _doctor("core")
    checks = source["checks"]
    assert isinstance(checks, list)
    for check in checks:
        assert isinstance(check, dict)
        check["status"] = "pass"
    if status != "pass":
        checks[0]["status"] = status
    source["overallStatus"] = status

    evidence = _adapt("doctor", source, exit_code=exit_code)
    assert verify_workerbee_profile_evidence(evidence) == evidence

    wrong = copy.deepcopy(evidence)
    provenance = wrong["provenance"]
    assert isinstance(provenance, dict)
    provenance["sourceExitCode"] = wrong_exit_code
    _assert_failed(
        "unsupported-schema",
        verify_workerbee_profile_evidence,
        _resign(wrong),
    )


def test_doctor_rejects_engine_probe_check_order_and_bad_recomputation() -> None:
    source, exit_code = _doctor("node")
    source["engineCapability"] = {"executable": "/private/bin/podman"}
    _assert_failed("source-shape-mismatch", _adapt, "doctor", source, exit_code=exit_code)

    source, exit_code = _doctor("node")
    source["checks"] = list(reversed(source["checks"]))  # type: ignore[arg-type]
    _assert_failed("source-shape-mismatch", _adapt, "doctor", source, exit_code=exit_code)

    source, _ = _doctor("node")
    source["overallStatus"] = "pass"
    _assert_failed("source-shape-mismatch", _adapt, "doctor", source, exit_code=0)

    source, exit_code = _doctor("node")
    source["canonicalName"] = "core"
    _assert_failed("source-shape-mismatch", _adapt, "doctor", source, exit_code=exit_code)


def test_status_separates_profile_generation_and_pseudonymizes_run() -> None:
    evidence = _adapt("status", _status())
    payload = evidence["payload"]
    assert payload == {
        "backend": "podman",
        "lifecycleStatus": "running",
        "phase": "running",
        "profile": "node",
        "profileGeneration": 7,
        "qualificationPosture": "source-validated/unqualified",
        "runRef": "sha256:068b3d1b8fde45ba6ad811be983a07a60f15ad3e539851dbc3851ccb1f35a7ce",
    }
    assert evidence["nodeIdentity"] is None
    assert "run_id" not in json.dumps(evidence)


def test_status_absent_deleted_and_invalid_relations() -> None:
    absent = _status()
    absent.update(backend=None, generation=None, phase=None, run_id=None, status="absent")
    payload = _adapt("status", absent)["payload"]
    assert payload["runRef"] is None  # type: ignore[index]
    assert payload["profileGeneration"] is None  # type: ignore[index]

    deleted = _status(status="deleted", phase="stopped")
    assert _adapt("status", deleted)["payload"]["lifecycleStatus"] == "deleted"  # type: ignore[index]

    for changed in (
        {"status": "deleted", "phase": "running"},
        {"status": "running", "phase": "starting"},
        {"backend": "unknown"},
        {"generation": 0},
        {"generation": True},
        {"exit_code": False},
    ):
        source = _status()
        source.update(changed)
        _assert_failed("source-shape-mismatch", _adapt, "status", source)


def test_logs_emit_only_owned_bounded_metadata() -> None:
    source = _logs()
    evidence = _adapt("logs", source)
    assert evidence["payload"] == {
        "component": "node-agent",
        "contentPolicy": "omitted",
        "lineCount": 2,
        "profile": "node",
        "runRef": "sha256:068b3d1b8fde45ba6ad811be983a07a60f15ad3e539851dbc3851ccb1f35a7ce",
        "sourceTruncated": False,
    }
    rendered = render_workerbee_profile_evidence_json(evidence)
    assert "private value" not in rendered
    assert "must-not-escape" not in rendered
    assert "lines" not in rendered


def test_logs_reject_wrong_component_and_size_limits() -> None:
    source = _logs()
    source["component"] = "controller"
    _assert_failed("source-shape-mismatch", _adapt, "logs", source)
    _assert_failed("size-limit", _adapt, "logs", _logs(["x"] * 201))
    _assert_failed("source-shape-mismatch", _adapt, "logs", _logs(["x" * 8193]))


@pytest.mark.parametrize(
    ("endpoint", "address_class", "scheme", "security", "port_present"),
    [
        ("http://controller.example", "hostname", "http", "cleartext", False),
        ("https://controller.example:9443", "hostname", "https", "tls", True),
        ("https://192.0.2.7", "ipv4", "https", "tls", False),
        ("https://[2001:db8::1]:9443", "ipv6", "https", "tls", True),
    ],
)
def test_bootstrap_connection_classification(
    endpoint: str,
    address_class: str,
    scheme: str,
    security: str,
    port_present: bool,
) -> None:
    evidence = _adapt("bootstrap", _bootstrap(endpoint=endpoint))
    controller = evidence["payload"]["connections"]["controller"]  # type: ignore[index]
    assert controller == {
        "addressClass": address_class,
        "portPresent": port_present,
        "presence": "present",
        "schemeClass": scheme,
        "securityClass": security,
    }
    rendered = render_workerbee_profile_evidence_json(evidence)
    assert "controller.example" not in rendered
    assert "192.0.2.7" not in rendered
    assert "2001:db8" not in rendered
    assert ":9443" not in rendered


def test_gateway_connection_projection_and_site_binding() -> None:
    evidence = _adapt("bootstrap", _gateway())
    payload = evidence["payload"]
    assert payload["backendId"] == "gateway"  # type: ignore[index]
    assert payload["siteId"] == "site-a"  # type: ignore[index]
    connections = payload["connections"]  # type: ignore[index]
    assert connections["transport"]["schemeClass"] == "tls"  # type: ignore[index]
    assert connections["controller"]["presence"] == "absent"  # type: ignore[index]


@pytest.mark.parametrize(
    "endpoint",
    [
        "HTTPS://controller.example",
        "https://Controller.example",
        "https://controller.example/",
        "https://controller.example/path",
        "https://controller.example?query=1",
        "https://controller.example#fragment",
        "https://user@controller.example",
        "https://controller.example:080",
        "https://controller.example:0",
        "https://controller.example:65536",
        "https://controller_example",
        "https://controller.example.",
        "https://999.1.1.1",
        "https://[2001:0db8::1]",
        "https://[2001:db8::1%25eth0]",
        "https://münchen.example",
    ],
)
def test_bootstrap_rejects_noncanonical_or_private_endpoint_metadata(endpoint: str) -> None:
    _assert_failed("unsafe-metadata", _adapt, "bootstrap", _bootstrap(endpoint=endpoint))


def test_bootstrap_node_observation_binding_and_generation_rules() -> None:
    unobserved = _adapt("bootstrap", _bootstrap())
    assert unobserved["nodeIdentity"] == {
        "generation": None,
        "generationProvenance": "unobserved",
        "identityMode": "explicit",
        "identityVersion": 1,
        "nodeId": "node-a",
    }
    observed = _adapt("bootstrap", _bootstrap(), observation=_observation())
    assert observed["nodeIdentity"]["generation"] == 11  # type: ignore[index]
    assert observed["nodeIdentity"]["generationProvenance"] == "k1s-controller-lease"  # type: ignore[index]

    _assert_failed(
        "node-observation-mismatch",
        _adapt,
        "bootstrap",
        _bootstrap(),
        observation=_observation(node_id="node-b"),
    )
    for generation in (0, -1, True, 1 << 63):
        _assert_failed(
            "node-observation-mismatch",
            _adapt,
            "bootstrap",
            _bootstrap(),
            observation=_observation(generation=generation),
        )


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("identityMode", "legacy"),
        ("identityVersion", 0),
        ("provenance", "caller-asserted"),
        ("apiVersion", "k1s.dev/node-lease-observation/v2"),
        ("kind", "Unknown"),
        ("schemaVersion", 2),
    ],
)
def test_bootstrap_rejects_untrusted_node_observation(key: str, value: object) -> None:
    observation = _observation()
    observation[key] = value
    _assert_failed(
        "node-observation-mismatch",
        _adapt,
        "bootstrap",
        _bootstrap(),
        observation=observation,
    )


def test_bootstrap_public_evidence_has_no_secret_or_private_verifier() -> None:
    source = _bootstrap()
    source_text = json.dumps(source)
    evidence = _adapt("bootstrap", source, observation=_observation())
    rendered = render_workerbee_profile_evidence_json(evidence)
    for forbidden in (
        "controller.example",
        "node.example",
        "127.0.0.1",
        "9109",
        "credential",
        "privateBootstrapDigest",
        "hostReceiptDigest",
        "socket",
        "path",
    ):
        assert forbidden not in rendered
    assert source_text != rendered


def test_source_duplicate_unknown_missing_type_null_and_order_cases_fail() -> None:
    duplicate = '{"apiVersion":"k1s.dev/v1alpha1","apiVersion":"x"}'
    _assert_failed("invalid-input", _adapt, "list", duplicate)

    for mutation in ("unknown", "missing", "type"):
        source = profile_catalog_payload()
        if mutation == "unknown":
            source["extra"] = None
        elif mutation == "missing":
            del source["kind"]
        else:
            source["schemaVersion"] = True
        _assert_failed("source-shape-mismatch", _adapt, "list", source)

    source = profile_catalog_payload()
    source["profiles"] = list(reversed(source["profiles"]))  # type: ignore[arg-type]
    _assert_failed("source-shape-mismatch", _adapt, "list", source)


def test_source_mode_exit_descriptor_and_operation_fail_closed() -> None:
    _assert_failed("source-exit-mismatch", _adapt, "list", profile_catalog_payload(), exit_code=1)
    _assert_failed("unsupported-operation", _adapt, "unknown", {})


def test_evidence_identity_known_answer_and_canonical_bytes() -> None:
    evidence = _adapt("status", _status())
    assert evidence["evidenceId"] == (
        "sha256:0cd2ed134f206a2a50a630fdf79588aca2823ace8684d33d4d12bf10f61c08d4"
    )
    rendered = render_workerbee_profile_evidence_json(evidence)
    assert rendered.endswith("\n") and not rendered.endswith("\n\n")
    assert " " not in rendered
    assert verify_workerbee_profile_evidence(rendered) == evidence

    tampered = copy.deepcopy(evidence)
    tampered["payload"]["profileGeneration"] = 8  # type: ignore[index]
    _assert_failed("unsupported-schema", verify_workerbee_profile_evidence, tampered)
    _assert_failed("invalid-input", verify_workerbee_profile_evidence, rendered.rstrip("\n"))


def test_evidence_rejects_unknown_missing_duplicate_and_forbidden_fields() -> None:
    evidence = _adapt("status", _status())
    changed = copy.deepcopy(evidence)
    changed["extra"] = None
    _assert_failed("unsupported-schema", verify_workerbee_profile_evidence, changed)
    changed = copy.deepcopy(evidence)
    del changed["kind"]
    _assert_failed("unsupported-schema", verify_workerbee_profile_evidence, changed)

    rendered = render_workerbee_profile_evidence_json(evidence)
    duplicate = rendered.replace(
        '"kind":"WorkerBeeProfileEvidence"',
        '"kind":"WorkerBeeProfileEvidence","kind":"WorkerBeeProfileEvidence"',
    )
    _assert_failed("invalid-input", verify_workerbee_profile_evidence, duplicate)

    bootstrap = _adapt("bootstrap", _bootstrap())
    changed = copy.deepcopy(bootstrap)
    changed["payload"]["credential"] = "secret"  # type: ignore[index]
    _assert_failed("unsupported-schema", verify_workerbee_profile_evidence, changed)

    doctor, exit_code = _doctor("core")
    changed = _adapt("doctor", doctor, exit_code=exit_code)
    changed["payload"]["checks"][0]["id"] = "unknown"  # type: ignore[index]
    _assert_failed("unsupported-schema", verify_workerbee_profile_evidence, _resign(changed))

    changed = _adapt("doctor", doctor, exit_code=exit_code)
    changed["provenance"]["sourceExitCode"] = False  # type: ignore[index]
    _assert_failed("unsupported-schema", verify_workerbee_profile_evidence, _resign(changed))

    changed = _adapt("bootstrap", _bootstrap())
    connection = changed["payload"]["connections"]["controller"]  # type: ignore[index]
    connection["schemeClass"] = None  # type: ignore[index]
    connection["securityClass"] = None  # type: ignore[index]
    _assert_failed("unsupported-schema", verify_workerbee_profile_evidence, _resign(changed))


def test_canonical_byte_rules_reject_float_non_nfc_control_and_wrong_newline() -> None:
    _assert_failed("invalid-input", _adapt, "list", '{"schemaVersion":1.0}\n')
    source = _logs(["e\u0301"])
    _assert_failed("invalid-input", _adapt, "logs", source)
    source = _logs(["line\u0001"])
    _assert_failed("invalid-input", _adapt, "logs", source)


def test_schema_constants_and_provenance_are_exact() -> None:
    evidence = _adapt("list", profile_catalog_payload())
    assert evidence["apiVersion"] == EVIDENCE_API_VERSION
    assert evidence["provenance"] == {
        "adapterApiVersion": ADAPTER_API_VERSION,
        "distribution": "k1s-core",
        "distributionVersion": "1.0.0",
        "sourceApiVersion": "k1s.dev/v1alpha1",
        "sourceExitCode": 0,
        "sourceKind": "ProfileCatalog",
        "sourceMode": "profile-list-json",
        "sourceSchemaVersion": 1,
    }
