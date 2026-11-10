#!/usr/bin/env python3
"""Install and smoke-test a k1s-core wheel with no package-index or network access."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

LOCK_RE = re.compile(
    r"^(?P<requirement>[^#\s][^#]*?)\s+--hash=sha256:(?P<digest>[0-9a-f]{64})"
    r"\s+#\s+(?P<filename>\S+\.whl)\s*$"
)
INDEX_ENV_KEYS = {
    "PIP_INDEX_URL",
    "PIP_EXTRA_INDEX_URL",
    "PIP_TRUSTED_HOST",
    "UV_INDEX_URL",
    "UV_EXTRA_INDEX_URL",
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _locked_wheels(lock: Path) -> dict[str, str]:
    wheels: dict[str, str] = {}
    for line_number, line in enumerate(lock.read_text(encoding="utf-8").splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = LOCK_RE.fullmatch(stripped)
        if match is None:
            raise ValueError(f"malformed lock entry at {lock}:{line_number}")
        filename = match.group("filename")
        if filename in wheels:
            raise ValueError(f"duplicate locked wheel: {filename}")
        wheels[filename] = match.group("digest")
    if not wheels:
        raise ValueError(f"empty wheel lock: {lock}")
    return wheels


def verify_wheelhouse(wheelhouse: Path, lock: Path) -> None:
    """Require the local wheelhouse to match the reviewed lock exactly."""
    expected = _locked_wheels(lock)
    actual = {path.name: _sha256(path) for path in wheelhouse.glob("*.whl") if path.is_file()}
    if actual != expected:
        missing = sorted(expected.keys() - actual.keys())
        extra = sorted(actual.keys() - expected.keys())
        changed = sorted(
            name for name in expected.keys() & actual.keys() if expected[name] != actual[name]
        )
        raise ValueError(
            f"wheelhouse does not match lock: missing={missing}, extra={extra}, changed={changed}"
        )


def offline_environment(*, home: Path, temporary: Path, guard: Path) -> dict[str, str]:
    """Return a credential-minimal environment with indexes and sockets disabled."""
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in INDEX_ENV_KEYS and "proxy" not in key.lower()
    }
    env.update(
        {
            "AE_RUNTIME_BACKEND": "stub",
            "HOME": str(home),
            "PIP_CONFIG_FILE": os.devnull,
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_NO_INDEX": "1",
            "PYTHONHASHSEED": "0",
            "PYTHONPATH": str(guard),
            "TMPDIR": str(temporary),
        }
    )
    return env


def require_outside_checkout(path: Path, source_root: Path) -> None:
    """Reject smoke work inside the source tree without conflating sibling paths."""

    resolved_path = path.resolve()
    resolved_source = source_root.resolve()
    if resolved_path == resolved_source or resolved_path.is_relative_to(resolved_source):
        raise ValueError("installed-artifact smoke unexpectedly ran inside the checkout")


def _run(argv: list[str], *, cwd: Path, env: dict[str, str]) -> None:
    rendered = list(argv)
    if "-c" in rendered:
        program_index = rendered.index("-c") + 1
        if program_index < len(rendered):
            rendered[program_index] = "<bounded-installed-smoke-program>"
    print("+", " ".join(rendered))
    subprocess.run(argv, cwd=cwd, env=env, check=True)  # noqa: S603


def _run_json(argv: list[str], *, cwd: Path, env: dict[str, str]) -> dict[str, object]:
    print("+", " ".join(argv))
    completed = subprocess.run(  # noqa: S603
        argv,
        cwd=cwd,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    value = json.loads(completed.stdout)
    if type(value) is not dict:
        raise ValueError(f"command did not return a JSON object: {argv}")
    return value


def _canonical_json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _strict_cri_smoke_documents() -> tuple[dict[str, object], dict[str, object]]:
    """Return synthetic, non-secret authorities for the installed pure planner."""

    owner_uid = os.getuid()
    owner_gid = os.getgid()
    intent: dict[str, object] = {
        "apiVersion": "k1s.dev/v1alpha1",
        "cni": {
            "bridgeName": "k1s0",
            "configRoot": "/etc/cni/net.d",
            "cniVersion": "1.0.0",
            "podCidr": "10.42.0.0/16",
            "requiredPlugins": ["bridge", "portmap", "firewall", "tuning", "loopback"],
            "sourceBinRoot": "/opt/k1s/cni-source",
            "targetBinRoot": "/opt/cni/bin",
        },
        "containerd": {
            "certsRoot": "/etc/containerd/certs.d",
            "configPath": "/etc/containerd/config.toml",
        },
        "cri": {
            "endpoint": "unix:///run/containerd/containerd.sock",
            "runtimeHandler": "runc",
        },
        "kind": "StrictCRIHostIntent",
        "nodeIdentity": None,
        "profileId": "artifact-core",
        "registry": {"mode": "off"},
        "role": "core",
        "schemaVersion": 1,
        "socketAccess": {"gid": owner_gid, "mode": "acl", "uid": owner_uid},
        "target": {
            "hostIdentitySha256": "sha256:" + "a" * 64,
            "ownerGid": owner_gid,
            "ownerUid": owner_uid,
            "targetId": "artifact-guest",
        },
    }

    def present(
        path: str,
        object_type: str,
        inode: int,
        *,
        digest: str | None = None,
        mode: str | None = None,
    ) -> dict[str, object]:
        return {
            "device": 7,
            "gid": 0,
            "inode": inode,
            "mode": mode or {"directory": "0755", "regular": "0755", "socket": "0660"}[object_type],
            "nlink": 2 if object_type == "directory" else 1,
            "path": path,
            "sha256": digest if object_type == "regular" else None,
            "size": 4096 if object_type == "regular" else 0,
            "state": "present",
            "type": object_type,
            "uid": 0,
        }

    def absent(path: str, inode: int) -> dict[str, object]:
        return {
            "parent": {
                "device": 7,
                "gid": 0,
                "inode": inode,
                "mode": "0755",
                "nlink": 2,
                "path": str(Path(path).parent),
                "type": "directory",
                "uid": 0,
            },
            "path": path,
            "state": "absent",
        }

    objects: dict[str, object] = {
        "cniConfigRoot": present("/etc/cni/net.d", "directory", 1),
        "cniSourceRoot": present("/opt/k1s/cni-source", "directory", 2),
        "cniTargetRoot": present("/opt/cni/bin", "directory", 3),
        "containerdCertsRoot": present("/etc/containerd/certs.d", "directory", 4),
        "containerdConfig": present(
            "/etc/containerd/config.toml",
            "regular",
            5,
            digest="sha256:" + "1" * 64,
            mode="0644",
        ),
        "criSocket": present("/run/containerd/containerd.sock", "socket", 6),
    }
    plugins = ("bridge", "portmap", "firewall", "tuning", "loopback")
    for offset, plugin in enumerate(plugins, 10):
        objects[f"cniSource:{plugin}"] = present(
            f"/opt/k1s/cni-source/{plugin}",
            "regular",
            offset,
            digest=f"sha256:{offset:064x}",
        )
        objects[f"cniTarget:{plugin}"] = absent(f"/opt/cni/bin/{plugin}", 3)
    for name in ("10-k1s.conflist", "99-loopback.conf"):
        objects[f"cniConfig:{name}"] = absent(f"/etc/cni/net.d/{name}", 1)

    facts: dict[str, object] = {
        "apiVersion": "k1s.dev/v1alpha1",
        "arch": "amd64",
        "containerd": {
            "configActive": True,
            "criApiVersion": "v1",
            "runtimeHandlers": ["runc"],
            "version": "1.7.24",
        },
        "hostIdentitySha256": "sha256:" + "a" * 64,
        "kind": "StrictCRIHostFacts",
        "objects": objects,
        "ownerGid": owner_gid,
        "ownerUid": owner_uid,
        "platform": "linux",
        "rootless": False,
        "schemaVersion": 1,
        "targetId": "artifact-guest",
    }
    return intent, facts


def validate_install(
    *, wheel: Path, wheelhouse: Path, lock: Path, work_root: Path, expected_version: str
) -> None:
    if work_root.exists():
        raise ValueError(f"work root already exists: {work_root}")
    work_root.mkdir(mode=0o700, parents=True)
    if work_root.stat().st_mode & 0o077:
        raise ValueError(f"work root is not owner-private: {work_root}")
    verify_wheelhouse(wheelhouse, lock)

    home = work_root / "home"
    temporary = work_root / "tmp"
    outside = work_root / "outside-checkout"
    guard = work_root / "network-guard"
    for path in (home, temporary, outside, guard):
        path.mkdir(mode=0o700)
    (guard / "sitecustomize.py").write_text(
        """import socket

def _deny(*_args, **_kwargs):
    raise RuntimeError("network access disabled by installed-artifact gate")

socket.create_connection = _deny
socket.getaddrinfo = _deny
socket.socket.connect = _deny
socket.socket.connect_ex = _deny
""",
        encoding="utf-8",
    )
    env = offline_environment(home=home, temporary=temporary, guard=guard)

    venv = work_root / "venv"
    _run([sys.executable, "-m", "venv", str(venv)], cwd=outside, env=env)
    python = venv / "bin" / "python"
    bin_dir = venv / "bin"
    source_root = Path(__file__).resolve().parents[1]
    _run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--no-index",
            "--find-links",
            str(wheelhouse),
            str(wheel),
        ],
        cwd=outside,
        env=env,
    )
    _run([str(python), "-m", "pip", "check"], cwd=outside, env=env)
    _run(
        [
            str(python),
            "-c",
            """from importlib import metadata, resources
import json
from pathlib import Path
import os
import sys
from dataclasses import replace
import ae
from ae.apishim.server import _openapi_v3_stub, _swagger_doc
from ae.kctl.profile_backend import build_execution_plan, portable_plan_digest
from ae.kctl.profile_adapter import (
    NODE_OBSERVATION_API_VERSION,
    ProfileAdapterError,
    adapt_workerbee_profile_evidence,
    load_workerbee_adapter_contract,
    render_workerbee_profile_evidence_json,
    verify_workerbee_profile_evidence,
)
from ae.kctl.profile_bootstrap import ProfileBootstrapError, ProfileBootstrapStore
from ae.kctl.profile_engine import EngineCapability, QUALIFICATION_POSTURE
from ae.kctl.profile_host_plan import PLAN_KIND
from ae.kctl.profile_paths import ProfileRoots
from ae.kctl.profile_state import FailureRecord, ProfileStateStore
from ae.kctl.profiles import (
    PROFILE_INPUT_NAMES,
    PROFILE_NAMES,
    InstalledProfileFacts,
    profile_catalog_payload,
    profile_doctor_payload,
    profile_plan_payload,
)
from ae.kctl.profile_supervisor import classify_identity, parse_proc_stat
from ae.runtime.cri.api.runtime.v1 import api_pb2, gogo_pb2
from ae.runtime.cri.api.runtime.v1 import api_pb2_grpc
from ae.storage.csi.api import csi_pb2, csi_pb2_grpc

expected = os.environ['K1S_EXPECTED_VERSION']
venv_root = Path(os.environ['K1S_VENV_ROOT']).resolve()
source_root = Path(os.environ['K1S_SOURCE_ROOT']).resolve()

def under(path, root):
    try:
        Path(path).resolve().relative_to(root)
    except ValueError:
        return False
    return True

def files_below(root):
    found = []
    for child in root.iterdir():
        if child.is_dir():
            found.extend(files_below(child))
        elif child.is_file():
            found.append(child)
    return found

assert ae.__version__ == expected
assert metadata.version('k1s-core') == expected
assert under(ae.__file__, venv_root)
distribution = metadata.distribution('k1s-core')
assert under(distribution.locate_file(''), venv_root)
installed_members = {str(item) for item in distribution.files or ()}
profile_members = {
    'ae/resources/profiles/profile-catalog-v1.json',
    'ae/resources/profiles/workerbee-adapter-v1.json',
}
for profile_member in profile_members:
    assert list(str(item) for item in distribution.files or ()).count(profile_member) == 1
assert not any(
    member == 'test_apishim_env.py'
    or member.startswith('test/')
    or member.startswith('tests/')
    for member in installed_members
)
assert 'k1s-workerbee-runtime' not in {
    item.metadata['Name'] for item in metadata.distributions()
}
assert metadata.packages_distributions().get('ae') == ['k1s-core']
assert all(not under(entry, source_root) for entry in sys.path if entry)
assert _swagger_doc()['info']['version'] == expected
assert _openapi_v3_stub()['info']['version'] == expected
assert api_pb2.DESCRIPTOR.dependencies[0] is gogo_pb2.DESCRIPTOR
assert api_pb2_grpc.api__pb2 is api_pb2
assert csi_pb2_grpc is not None
assert csi_pb2_grpc.csi__pb2 is csi_pb2
assert PLAN_KIND == 'StrictCRIHostPlan'

resource_root = resources.files('ae.resources')
resource_files = files_below(resource_root)
resource_relative = {
    str(Path(item).relative_to(Path(resource_root))) for item in resource_files
}
resource_data = {
    name
    for name in resource_relative
    if name.endswith(('.sql', '.html', '.txt', '.json'))
}
expected_html = {
    'observability/dashboard.html',
    'observability/docs.html',
    'observability/redoc.html',
    'observability/swagger.html',
}
assert len([name for name in resource_data if name.startswith('sql/apishim/')]) == 13
assert len([name for name in resource_data if name.startswith('sql/controller/')]) == 64
assert {name for name in resource_data if name.endswith('.html')} == expected_html
assert {name for name in resource_data if name.endswith('.txt')} == {
    'ingress/caddy_site.txt'
}
assert {name for name in resource_data if name.endswith('.json')} == {
    'profiles/profile-catalog-v1.json',
    'profiles/workerbee-adapter-v1.json',
}
assert len(resource_data) == 84
assert not any(name.endswith('.md') for name in resource_relative)
for item in resource_files:
    if str(item).endswith(('.sql', '.html', '.txt', '.json')):
        assert item.read_bytes()

profile_path = resource_root.joinpath('profiles', 'profile-catalog-v1.json')
assert under(profile_path, venv_root)
profile_raw = profile_path.read_bytes()
profile_text = profile_raw.decode('utf-8')
profile_catalog = json.loads(profile_text)
assert profile_text == json.dumps(
    profile_catalog, ensure_ascii=False, indent=2, sort_keys=True
) + '\\n'
assert profile_catalog == profile_catalog_payload()
assert tuple(profile['name'] for profile in profile_catalog['profiles']) == PROFILE_NAMES
assert PROFILE_INPUT_NAMES == (
    'local', 'core', 'edge-gateway', 'node', 'edge-dev', 'core-ha',
    'edge', 'core-edge', 'edge-core'
)
profile_by_name = {profile['name']: profile for profile in profile_catalog['profiles']}
assert profile_by_name['edge-gateway']['components'] == ['gateway']
assert profile_by_name['core-ha']['contractPosture'] == 'preview'
assert profile_by_name['core-ha']['productionEligible'] is False
assert profile_by_name['local']['executionStatus'] == 'portable'
assert profile_by_name['core']['executionStatus'] == 'portable'
assert profile_by_name['edge-gateway']['executionStatus'] == 'portable'
assert profile_by_name['node']['executionStatus'] == 'portable'
assert profile_by_name['local']['qualificationPosture'] == QUALIFICATION_POSTURE
assert profile_by_name['core']['qualificationPosture'] == QUALIFICATION_POSTURE
assert profile_by_name['edge-gateway']['qualificationPosture'] == QUALIFICATION_POSTURE
assert profile_by_name['node']['qualificationPosture'] == QUALIFICATION_POSTURE

adapter_path = resource_root.joinpath('profiles', 'workerbee-adapter-v1.json')
assert under(adapter_path, venv_root)
adapter_raw = adapter_path.read_bytes()
adapter_text = adapter_raw.decode('utf-8')
adapter_contract = json.loads(adapter_text)
assert adapter_text == json.dumps(
    adapter_contract, ensure_ascii=False, indent=2, sort_keys=True
) + '\\n'
assert [item.name for item in load_workerbee_adapter_contract().operations] == [
    'list', 'doctor', 'plan', 'status', 'logs', 'bootstrap'
]

class StaticProbe:
    def inspect(self):
        return InstalledProfileFacts(
            python_version=(3, 11),
            platform='linux',
            ae_importable=True,
            distribution_version=expected,
            k1s_console_present=True,
        )

doctor_source, doctor_exit = profile_doctor_payload('core', StaticProbe())
adapter_sources = {
    'list': (profile_catalog_payload(), 0, None),
    'doctor': (doctor_source, doctor_exit, None),
    'plan': (profile_plan_payload('node'), 0, None),
    'status': ({
        'action': 'status',
        'backend': 'podman',
        'exit_code': 0,
        'generation': 7,
        'phase': 'running',
        'profile': 'node',
        'qualification_posture': 'source-validated/unqualified',
        'run_id': 'r1-0123456789abcdef0123456789abcdef',
        'status': 'running',
    }, 0, None),
    'logs': ({
        'apiVersion': 'k1s.dev/v1alpha1',
        'component': 'node-agent',
        'kind': 'ProfileLogSnapshot',
        'lines': ['secret-like material omitted from evidence'],
        'profile': 'node',
        'runId': 'r1-0123456789abcdef0123456789abcdef',
        'truncated': False,
    }, 0, None),
    'bootstrap': ({
        'advertiseEndpoint': 'https://node.example:9109',
        'backendId': 'podman',
        'bootstrapId': 'b1-0123456789abcdef0123456789abcdef',
        'controllerUrl': 'https://controller.example:9443',
        'identityVersion': 1,
        'listenHost': '127.0.0.1',
        'listenPort': 9109,
        'nodeId': 'node-a',
        'profile': 'node',
        'siteId': None,
        'transportUrl': None,
    }, 0, {
        'apiVersion': NODE_OBSERVATION_API_VERSION,
        'generation': 11,
        'identityMode': 'explicit',
        'identityVersion': 1,
        'kind': 'NodeLeaseObservation',
        'nodeId': 'node-a',
        'provenance': 'k1s-controller-lease',
        'schemaVersion': 1,
    }),
}
for operation, (source, exit_code, observation) in adapter_sources.items():
    evidence = adapt_workerbee_profile_evidence(
        operation,
        source,
        source_exit_code=exit_code,
        node_observation=observation,
    )
    rendered = render_workerbee_profile_evidence_json(evidence)
    assert verify_workerbee_profile_evidence(rendered) == evidence
    assert 'controller.example' not in rendered
    assert 'node.example' not in rendered
    assert 'secret-like material' not in rendered

tampered = dict(evidence)
tampered['evidenceId'] = 'sha256:' + '0' * 64
try:
    verify_workerbee_profile_evidence(tampered)
except ProfileAdapterError as exc:
    assert exc.code == 'unsupported-schema'
else:
    raise AssertionError('installed adapter accepted a tampered evidence identity')

# Exercise only the additive installed state contract in an owner-private disposable root.
# This never launches a component, resolves a backend, or touches the real HOME/XDG roots.
profile_smoke_root = Path(os.environ['K1S_PROFILE_SMOKE_ROOT']).resolve()
profile_env = {
    'XDG_CONFIG_HOME': str(profile_smoke_root / 'config'),
    'XDG_STATE_HOME': str(profile_smoke_root / 'state'),
    'XDG_DATA_HOME': str(profile_smoke_root / 'data'),
    'XDG_CACHE_HOME': str(profile_smoke_root / 'cache'),
}
roots = ProfileRoots.from_environment(profile_env)
store = ProfileStateStore(roots, 'local')
assert store.inspect() is None
assert not roots.state.exists()
synthetic_engine = EngineCapability(
    backend='podman',
    executable=Path(sys.executable).resolve(),
    executable_device=0,
    executable_inode=1,
    version='installed-source-smoke',
    rootless=True,
)
plan_digest = portable_plan_digest('local', synthetic_engine)
run = store.begin_run(backend_id='podman', plan_digest=plan_digest)
plan = build_execution_plan(
    'local', synthetic_engine, run, store, python_executable=Path(sys.executable).resolve()
)
assert plan.digest == plan_digest
assert [component.component_id for component in plan.components] == [
    'controller', 'node-local'
]
expected_python = Path(sys.executable).resolve()
assert all(Path(component.argv[0]) == expected_python for component in plan.components)
assert not under(expected_python, source_root)
assert all('HTTP_PROXY' not in dict(component.child_environment) for component in plan.components)
assert all(
    target.port in {9108, 9109, 9110}
    for component in plan.components
    for target in component.readiness
)
run = store.transition(
    run.run_id,
    'failed',
    expected_revision=run.revision,
    failure=FailureRecord('installed-smoke', 'Synthetic installed state smoke.'),
)
run = store.transition(run.run_id, 'stopping', expected_revision=run.revision)

class _NoopCleaner:
    def cleanup_resource(self, _receipt):
        raise AssertionError('installed state smoke created no runtime resource')

run = store.cleanup_owned(run.run_id, _NoopCleaner())
run = store.settle_components(run.run_id, expected_revision=run.revision)
run = store.transition(
    run.run_id,
    'stopped',
    expected_revision=run.revision,
    cleanup_complete=True,
)
assert store.inspect() == run
assert run.cleanup_complete is True
assert run.resource_receipts == ()
assert run.secret_receipts == ()
assert classify_identity is not None
assert parse_proc_stat is not None
assert under(roots.state, profile_smoke_root)
assert roots.state.stat().st_mode & 0o777 == 0o700

# Canonical edge/node construction uses only synthetic owner-private inputs and injected
# engine identity. It never starts a process, opens a socket, or calls a backend.
node_private = 'installed-private-node-value'
node_credential = profile_smoke_root / 'node-credential'
node_credential.write_text(node_private, encoding='utf-8')
node_credential.chmod(0o600)
node_bootstraps = ProfileBootstrapStore(roots, 'node')
node_bootstrap = node_bootstraps.create_node(
    node_id='node-installed',
    controller_url='https://controller.example.invalid:9443',
    runtime_backend='podman',
    listen_host='127.0.0.1',
    listen_port=9109,
    advertise_endpoint='https://node.example.invalid:9109',
    credential_file=node_credential,
)
node_public = node_bootstrap.public_projection()
assert node_public['identityVersion'] == 1
assert node_public['nodeId'] == 'node-installed'
assert node_private not in json.dumps(node_public, sort_keys=True)
assert str(node_credential) not in json.dumps(node_public, sort_keys=True)
node_digest = portable_plan_digest(
    'node', synthetic_engine, bootstrap=node_bootstrap,
    python_executable=Path(sys.executable).resolve(),
)
private_variant = replace(
    node_bootstrap,
    credential='installed-private-node-other',
    private_record_digest='sha256:' + 'f' * 64,
)
assert portable_plan_digest(
    'node', synthetic_engine, bootstrap=private_variant,
    python_executable=Path(sys.executable).resolve(),
) == node_digest
try:
    node_bootstraps.revalidate(private_variant)
except ProfileBootstrapError:
    pass
else:
    raise AssertionError('private bootstrap drift was not rejected')
node_store = ProfileStateStore(roots, 'node')
node_run = node_store.begin_run(
    backend_id='podman',
    plan_digest=node_digest,
    bootstrap_id=node_bootstrap.bootstrap_id,
    private_bootstrap_digest=node_bootstrap.private_record_digest,
)
node_plan = build_execution_plan(
    'node', synthetic_engine, node_run, node_store,
    python_executable=Path(sys.executable).resolve(),
    bootstrap=node_bootstrap,
    credential_path=None,
)
assert [item.component_id for item in node_plan.components] == ['node-agent']
assert '--node-id' in node_plan.components[0].argv
assert node_plan.components[0].argv[node_plan.components[0].argv.index('--node-id') + 1] == (
    'node-installed'
)
assert node_private not in repr(node_plan)
assert node_bootstrap.private_record_digest not in repr(node_plan)
assert str(node_bootstrap.path) not in repr(node_plan)
assert node_run.secret_receipts == ()
assert list(node_store.layout.for_run(node_run.run_id).run_secrets.iterdir()) == []

gateway_private = 'installed-private-gateway-value'
gateway_credential = profile_smoke_root / 'gateway-credential'
gateway_credential.write_text(gateway_private, encoding='utf-8')
gateway_credential.chmod(0o600)
gateway_bootstraps = ProfileBootstrapStore(roots, 'edge-gateway')
gateway_bootstrap = gateway_bootstraps.create_edge_gateway(
    site_id='site-installed',
    node_id='gateway-installed',
    transport_url='tls://nats.example.invalid:4222',
    credential_file=gateway_credential,
)
gateway_digest = portable_plan_digest(
    'edge-gateway', None, bootstrap=gateway_bootstrap,
    python_executable=Path(sys.executable).resolve(),
)
gateway_store = ProfileStateStore(roots, 'edge-gateway')
gateway_run = gateway_store.begin_run(
    backend_id='gateway',
    plan_digest=gateway_digest,
    bootstrap_id=gateway_bootstrap.bootstrap_id,
    private_bootstrap_digest=gateway_bootstrap.private_record_digest,
)
gateway_run, _ = gateway_store.create_secret(
    gateway_run.run_id,
    'gateway-credentials',
    gateway_private.encode('utf-8'),
    expected_revision=gateway_run.revision,
)
gateway_secret = gateway_store.verify_secret(
    gateway_run.run_id, 'gateway-credentials', gateway_private.encode('utf-8')
)
gateway_plan = build_execution_plan(
    'edge-gateway', None, gateway_run, gateway_store,
    python_executable=Path(sys.executable).resolve(),
    bootstrap=gateway_bootstrap,
    credential_path=gateway_secret,
)
assert gateway_plan.engine is None
assert [item.component_id for item in gateway_plan.components] == ['gateway']
assert gateway_private not in repr(gateway_plan)

static_root = resources.files('ae.observability').joinpath('static/vendor')
static_files = {item.name: item for item in static_root.iterdir() if item.is_file()}
assert set(static_files) == {'xterm.css', 'xterm.js', 'xterm-addon-fit.js'}
assert all(item.read_bytes() for item in static_files.values())

scripts = {
    entry.name
    for entry in distribution.entry_points
    if entry.group == 'console_scripts'
}
assert scripts == {'ae', 'ae-rotate-certs', 'k1s', 'ae-node', 'ae-gateway', 'ae-worker-stub'}
""",
        ],
        cwd=outside,
        env={
            **env,
            "K1S_EXPECTED_VERSION": expected_version,
            "K1S_PROFILE_SMOKE_ROOT": str(outside / "profile-state-smoke"),
            "K1S_SOURCE_ROOT": str(source_root),
            "K1S_VENV_ROOT": str(venv.resolve()),
        },
    )
    for command in (
        ("ae", "--help"),
        ("ae", "version"),
        ("k1s", "--help"),
        ("ae-rotate-certs", "--help"),
        ("ae-node", "--help"),
        ("ae-gateway", "--help"),
        ("ae-worker-stub", "--help"),
    ):
        if not (bin_dir / command[0]).is_file():
            raise ValueError(f"installed console script is missing: {command[0]}")
        _run([str(bin_dir / command[0]), *command[1:]], cwd=outside, env=env)

    catalog = _run_json(
        [str(bin_dir / "k1s"), "profile", "list", "--output", "json"],
        cwd=outside,
        env=env,
    )
    catalog_profiles = catalog.get("profiles")
    if type(catalog_profiles) is not list or [
        profile.get("name") if type(profile) is dict else None for profile in catalog_profiles
    ] != ["local", "core", "edge-gateway", "node", "edge-dev", "core-ha"]:
        raise ValueError("installed profile catalog output differs")
    for profile_name in (
        "local",
        "core",
        "edge-gateway",
        "node",
        "edge-dev",
        "core-ha",
        "edge",
        "core-edge",
        "edge-core",
    ):
        plan = _run_json(
            [str(bin_dir / "k1s"), "profile", "plan", profile_name, "--output", "json"],
            cwd=outside,
            env=env,
        )
        portable_names = {"local", "core", "core-edge", "edge-gateway", "node"}
        expected_status = "portable" if profile_name in portable_names else "deferred"
        if plan.get("mutating") is not False or plan.get("executionStatus") != expected_status:
            raise ValueError(f"installed profile plan is not static: {profile_name}")
        if (
            profile_name in portable_names
            and plan.get("qualificationPosture") != "source-validated/unqualified"
        ):
            raise ValueError(
                f"installed portable profile plan overclaims qualification: {profile_name}"
            )
    for profile_name in ("local", "core", "edge-gateway", "node", "edge-dev", "core-ha"):
        doctor = _run_json(
            [str(bin_dir / "k1s"), "profile", "doctor", profile_name, "--output", "json"],
            cwd=outside,
            env=env,
        )
        if doctor.get("mutating") is not False:
            raise ValueError(f"installed profile doctor is not static: {profile_name}")

    host_plan_root = outside / "strict-cri-host-plan"
    host_plan_root.mkdir(mode=0o700)
    intent_value, facts_value = _strict_cri_smoke_documents()
    intent_path = host_plan_root / "intent.json"
    facts_path = host_plan_root / "facts.json"
    intent_raw = _canonical_json_bytes(intent_value)
    facts_raw = _canonical_json_bytes(facts_value)
    intent_path.write_bytes(intent_raw)
    facts_path.write_bytes(facts_raw)
    intent_path.chmod(0o600)
    facts_path.chmod(0o600)
    before_host_plan = {
        item.name: (item.stat().st_mode, item.stat().st_size, item.stat().st_mtime_ns)
        for item in host_plan_root.iterdir()
    }
    host_plan = _run_json(
        [
            str(bin_dir / "k1s"),
            "profile",
            "host",
            "plan",
            "strict-cri",
            "--intent",
            str(intent_path),
            "--expect-intent-sha256",
            f"sha256:{hashlib.sha256(intent_raw).hexdigest()}",
            "--facts",
            str(facts_path),
            "--expect-facts-sha256",
            f"sha256:{hashlib.sha256(facts_raw).hexdigest()}",
            "--output",
            "json",
        ],
        cwd=outside,
        env=env,
    )
    after_host_plan = {
        item.name: (item.stat().st_mode, item.stat().st_size, item.stat().st_mtime_ns)
        for item in host_plan_root.iterdir()
    }
    if before_host_plan != after_host_plan or set(after_host_plan) != {"intent.json", "facts.json"}:
        raise ValueError("installed strict-CRI host plan mutated its exact inputs")
    if (
        host_plan.get("kind") != "StrictCRIHostPlan"
        or host_plan.get("mutating") is not False
        or host_plan.get("qualificationPosture") != "source-validated/unqualified"
    ):
        raise ValueError("installed strict-CRI host plan contract differs")
    rendered_host_plan = json.dumps(host_plan, sort_keys=True)
    if str(intent_path) in rendered_host_plan or str(facts_path) in rendered_host_plan:
        raise ValueError("installed strict-CRI host plan disclosed an authority path")

    installed_transaction_program = r"""import hashlib
import json
import os
import stat
import threading
from pathlib import Path

from ae.kctl.profile_bootstrap import ProfileBootstrapStore, bootstrap_public_digest
from ae.kctl.profile_host_helper import (
    HelperRequest,
    StrictCRIHostHelperEngine,
    helper_implementation_digest,
    read_protocol_message,
    write_protocol_message,
)
from ae.kctl.profile_host_plan import (
    StrictCRIHostAuthorities,
    build_strict_cri_host_plan,
    canonical_json_bytes,
    strict_cri_action_materials,
)
from ae.kctl.profile_host_transaction import (
    PipeHostHelperClient,
    StrictCRIHostTransactionManager,
)
from ae.kctl.profile_node_adapter import StrictCRINodeAdapter, StrictCRINodeInputs
from ae.kctl.profile_paths import ProfileRoots


def digest(raw):
    return 'sha256:' + hashlib.sha256(raw).hexdigest()


root = Path(os.environ['K1S_STRICT_TRANSACTION_SMOKE_ROOT']).resolve()
root.mkdir(mode=0o700)
bootstrap_root = root / 'bootstrap-xdg'
profile_roots = ProfileRoots.from_environment({
    'XDG_CONFIG_HOME': str(bootstrap_root / 'config'),
    'XDG_STATE_HOME': str(bootstrap_root / 'state'),
    'XDG_DATA_HOME': str(bootstrap_root / 'data'),
    'XDG_CACHE_HOME': str(bootstrap_root / 'cache'),
})
credential = root / 'node-token'
credential.write_text('installed-strict-node-token', encoding='utf-8')
credential.chmod(0o600)
bootstrap_store = ProfileBootstrapStore(profile_roots, 'node')
bootstrap = bootstrap_store.create_node(
    node_id='node-installed-strict',
    controller_url='https://controller.example.invalid:9443',
    runtime_backend='strict-cri',
    listen_host='127.0.0.1',
    listen_port=9109,
    advertise_endpoint='https://node.example.invalid:9109',
    credential_file=credential,
)

intent = json.loads(Path(os.environ['K1S_HOST_INTENT']).read_text(encoding='utf-8'))
facts = json.loads(Path(os.environ['K1S_HOST_FACTS']).read_text(encoding='utf-8'))
intent['profileId'] = 'artifact-node'
intent['role'] = 'node'
intent['nodeIdentity'] = {
    'bootstrapId': bootstrap.bootstrap_id,
    'bootstrapPublicDigest': bootstrap_public_digest(bootstrap),
    'identityVersion': 1,
    'nodeId': bootstrap.node_id,
}
authority_input = root / 'input'
authority_input.mkdir(mode=0o700)
intent_path = authority_input / 'intent.json'
facts_path = authority_input / 'facts.json'
intent_raw = canonical_json_bytes(intent)
facts_raw = canonical_json_bytes(facts)
intent_path.write_bytes(intent_raw)
facts_path.write_bytes(facts_raw)
intent_path.chmod(0o600)
facts_path.chmod(0o600)
plan = build_strict_cri_host_plan(
    intent,
    facts,
    intent_digest=digest(intent_raw),
    facts_digest=digest(facts_raw),
)
plan_path = authority_input / 'plan.json'
plan_raw = canonical_json_bytes(plan)
plan_path.write_bytes(plan_raw)
plan_path.chmod(0o600)


class Boundary:
    def __init__(self):
        self.calls = []

    def execute(
        self,
        *,
        operation,
        transaction_id,
        transaction_root_fd,
        material,
        rollback_authorization_digest,
    ):
        info = os.fstat(transaction_root_fd)
        assert stat.S_ISDIR(info.st_mode)
        assert stat.S_IMODE(info.st_mode) == 0o700
        self.calls.append((operation, material.index, transaction_id))
        if operation == 'rollback':
            assert rollback_authorization_digest is not None
        else:
            assert rollback_authorization_digest is None
        return {
            'snapshot': material.expected_before_digest,
            'apply': material.desired_after_digest,
            'rollback': material.expected_before_digest,
            'verify': material.desired_after_digest,
        }[operation]


request_read, request_write = os.pipe()
response_read, response_write = os.pipe()
boundary = Boundary()
engine = StrictCRIHostHelperEngine(boundary)
materials = strict_cri_action_materials(
    StrictCRIHostAuthorities(
        intent=intent,
        facts=facts,
        plan=plan,
        intent_digest=digest(intent_raw),
        facts_digest=digest(facts_raw),
        plan_file_digest=digest(plan_raw),
        plan_digest=plan['planDigest'],
    )
)
expected_calls = (2 * len(materials)) + sum(item.rollback != 'none' for item in materials)
server_errors = []


def serve():
    try:
        for _index in range(expected_calls):
            request = HelperRequest.from_payload(read_protocol_message(request_read))
            write_protocol_message(response_write, engine.handle(request).to_payload())
    except BaseException as error:
        server_errors.append(error)


thread = threading.Thread(target=serve, daemon=True)
thread.start()
transaction_root = root / 'private-transaction'
transaction_root.mkdir(mode=0o700)
manager = StrictCRIHostTransactionManager(
    PipeHostHelperClient(request_write, response_read)
)
transaction_id = 'ht1-' + ('4' * 32)
target_id = plan['target']['targetId']
prepare = manager.prepare(
    intent_path=intent_path,
    expected_intent_digest=digest(intent_raw),
    facts_path=facts_path,
    expected_facts_digest=digest(facts_raw),
    plan_path=plan_path,
    expected_plan_file_digest=digest(plan_raw),
    expected_plan_digest=plan['planDigest'],
    transaction_root=transaction_root,
    transaction_id=transaction_id,
    confirmation=f"{target_id}:{plan['planDigest']}",
    expected_helper_identity_digest=helper_implementation_digest(),
)
assert prepare.status == 'prepared'


class Verifier:
    def verify(self, authorities, receipt):
        del receipt
        return tuple(
            material.desired_after_digest
            for material in strict_cri_action_materials(authorities)
        )


adapter = StrictCRINodeAdapter(
    bootstrap_store=bootstrap_store,
    verifier=Verifier(),
    python_executable=Path(os.environ['K1S_INSTALLED_PYTHON']).resolve(),
)
authority = adapter.resolve(
    bootstrap,
    inputs=StrictCRINodeInputs(
        plan_path=plan_path,
        expected_plan_file_digest=digest(plan_raw),
        expected_plan_digest=plan['planDigest'],
        receipt_path=transaction_root / 'receipts' / f'{transaction_id}.applied.json',
        expected_receipt_digest=prepare.receipt_digest,
    ),
)
public = authority.public_projection()
assert public['nodeId'] == 'node-installed-strict'
assert public['planDigest'] == plan['planDigest']
assert public['receiptDigest'] == prepare.receipt_digest
assert public['qualificationPosture'] == 'source-validated/unqualified'
assert bootstrap.credential not in repr(authority)
assert bootstrap.private_record_digest not in repr(authority)
assert str(transaction_root) not in json.dumps(public, sort_keys=True)

rollback = manager.rollback(
    transaction_root=transaction_root,
    transaction_id=transaction_id,
    expected_applied_receipt_digest=prepare.receipt_digest,
    confirmation=f'{target_id}:{transaction_id}:{prepare.receipt_digest}',
    expected_helper_identity_digest=helper_implementation_digest(),
)
assert rollback.status == 'rolled-back'
thread.join(timeout=10)
assert not thread.is_alive()
assert not server_errors
assert len(boundary.calls) == expected_calls
os.close(request_read)
os.close(request_write)
os.close(response_read)
os.close(response_write)
"""
    _run(
        [str(python), "-c", installed_transaction_program],
        cwd=outside,
        env={
            **env,
            "K1S_HOST_FACTS": str(facts_path),
            "K1S_HOST_INTENT": str(intent_path),
            "K1S_INSTALLED_PYTHON": str(python),
            "K1S_STRICT_TRANSACTION_SMOKE_ROOT": str(outside / "strict-cri-transaction-smoke"),
        },
    )

    manifest = outside / "artifact-smoke.yaml"
    manifest.write_text(
        """apiVersion: ae.dev/v1alpha1
kind: Deployment
metadata:
  name: artifact-smoke
spec:
  replicas: 1
  image: example.invalid/artifact-smoke:1
""",
        encoding="utf-8",
    )
    _run([str(bin_dir / "ae"), "plan", "-f", str(manifest), "--json"], cwd=outside, env=env)

    shutil.rmtree(guard)
    require_outside_checkout(outside, source_root)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--wheelhouse", type=Path, required=True)
    parser.add_argument("--requirements-lock", type=Path, required=True)
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--expected-version", required=True)
    args = parser.parse_args()
    validate_install(
        wheel=args.wheel.resolve(),
        wheelhouse=args.wheelhouse.resolve(),
        lock=args.requirements_lock.resolve(),
        work_root=args.work_root.resolve(),
        expected_version=args.expected_version,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
