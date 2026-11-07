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


def _run(argv: list[str], *, cwd: Path, env: dict[str, str]) -> None:
    print("+", " ".join(argv))
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
import ae
from ae.apishim.server import _openapi_v3_stub, _swagger_doc
from ae.kctl.profile_backend import build_execution_plan, portable_plan_digest
from ae.kctl.profile_engine import EngineCapability, QUALIFICATION_POSTURE
from ae.kctl.profile_paths import ProfileRoots
from ae.kctl.profile_state import FailureRecord, ProfileStateStore
from ae.kctl.profiles import PROFILE_INPUT_NAMES, PROFILE_NAMES, profile_catalog_payload
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
profile_member = 'ae/resources/profiles/profile-catalog-v1.json'
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
    'profiles/profile-catalog-v1.json'
}
assert len(resource_data) == 83
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
assert profile_by_name['local']['qualificationPosture'] == QUALIFICATION_POSTURE
assert profile_by_name['core']['qualificationPosture'] == QUALIFICATION_POSTURE

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
            "K1S_SOURCE_ROOT": str(Path(__file__).resolve().parents[1]),
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
        portable_names = {"local", "core", "core-edge"}
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
    if str(Path.cwd().resolve()) in str(outside.resolve()):
        raise ValueError("installed-artifact smoke unexpectedly ran inside the checkout")


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
