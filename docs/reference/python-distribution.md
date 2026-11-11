# Python Distribution and Compatibility

The installable Python distribution is `k1s-core` beginning with `0.1.8a1`.
This distribution identity matches the K1s core runtime consumed by local
WorkerBee wheelhouses. The rename does not rename the runtime's established
Python or command surfaces.

## Install and inspect

Install `k1s-core` from an approved package index or exact reviewed artifact:

```text
python -m pip install k1s-core
k1s --help
k1s profile list
k1s profile doctor core
k1s profile plan core
k1s profile doctor edge-gateway
k1s profile plan edge-gateway
k1s profile doctor node
k1s profile plan node
```

`k1s` is the primary installed package command. `ae` remains the installed
advanced compatibility and engineering surface. The profile inspection above
is deterministic, non-mutating, and reports the
`source-validated/unqualified` posture; it does not start a runtime, prepare a
host, or establish live WorkerBee, CRI/containerd, provider, or deployment
qualification. Make targets, `python -m ae.*`, and repository paths under
`scripts/`, `specs/`, `state/`, or `ops/` are source-checkout development
material rather than wheel-provided commands.

## Preserved compatibility surfaces

- Python imports remain under `ae`, including `import ae` and all
  `ae.runtime.cri` modules.
- The primary operator command remains `k1s`.
- `k1s profile list|doctor|plan` provides a deterministic, non-mutating
  installed contract for the canonical `local`, `core`, `edge-gateway`,
  `node`, `edge-dev`, and `core-ha` roles. Compatibility aliases remain
  inspectable, but `edge` and `edge-core` resolve only to development-only
  `edge-dev`; they never represent the production `edge-gateway` role.
- Existing commands `ae`, `ae-rotate-certs`, `ae-node`, `ae-gateway`, and
  `ae-worker-stub` remain installed by `k1s-core`.
- `k1s profile host plan strict-cri` is an installed, deterministic,
  non-mutating command. It consumes two explicit canonical digest-pinned files
  and reports `source-validated/unqualified`; it never probes or prepares a
  host and does not add another console entry point.
- `k1s profile host prepare|rollback strict-cri` uses the existing `k1s`
  console, explicit authority digests, an owner-private journal, and
  caller-provided helper pipe descriptors. It does not install a seventh
  helper console or auto-launch privilege.
- CRI protobuf modules remain under
  `ae.runtime.cri.api.runtime.v1`. The gogo descriptor keeps its canonical
  `github.com/gogo` protobuf identity internally, but no top-level `github`
  Python package is shipped.

The previous distribution requirement `ae` is not an alias for `k1s-core`.
Dependency declarations and installation commands must migrate to
`k1s-core`. Runtime imports and scripts do not require migration.

## Runtime package ownership

`k1s-core` is the only distribution that owns the `ae` package. The wheel
contains every `ae` Python module, the package-local CRI and CSI generated
modules, the SQL and HTML resources loaded through `ae.resources`, the Caddy
site template, the canonical static profile catalog, and the three vendored
terminal assets used by the dashboard's offline fallback. Package
documentation, repository tests, scripts, examples, operations files, charts,
and environment files are not runtime wheel payload.

The release validator compares the source-owned module and resource inventory
with the wheel and source distribution. It rejects duplicate or unsafe archive
paths, links and special members, extra distribution payloads, and incomplete
or inaccurate wheel `RECORD` entries. Every wheel file other than `RECORD`
must have its exact SHA-256 and byte size recorded; `RECORD` itself has empty
hash and size fields.

The former `scripts/build_workerbee_runtime_wheel.py` path is intentionally
removed. It attempted to create a second `k1s-workerbee-runtime` distribution
containing another copy of `ae`, and it was incompatible with the canonical
dynamic version source. WorkerBee consumes the same reviewed `k1s-core` wheel;
there is no second v1 runtime wheel or package identity.

The installed-artifact gate resolves `ae`, distribution metadata, console
scripts, protobuf modules, and runtime resources from a fresh virtual
environment outside the checkout. It disables package indexes, proxy/index
configuration, Python socket connections, and source-tree `PYTHONPATH`
fallback. Safe help, version, import, resource, and stub-only planning checks
run without starting Docker, containerd, CRI, CSI, Kubernetes, or a service.

The canonical profile catalog is
`ae/resources/profiles/profile-catalog-v1.json`. Its source, wheel, and source
distribution bytes must match; it has one exact wheel `RECORD` row with the
correct SHA-256 and size. The installed-artifact gate resolves it from a fresh
virtual environment outside the checkout, compares its schema and role
contract with the Python API, and exercises non-mutating `profile list`,
`doctor`, and `plan` commands with indexes and network sockets disabled.

The same profile resource directory contains exactly one additional JSON
contract, `workerbee-adapter-v1.json`. The wheel owns its exact canonical bytes
and one `RECORD` row together with `ae.kctl.profile_adapter`; wheel and source
distribution validators compare both resources to source. This adapter remains
part of the sole `k1s-core` distribution and adds no seventh console or second
WorkerBee runtime package. The strict installed gate exercises all six
synthetic projections, evidence identity verification, redaction, and tamper
rejection outside the checkout without opening a service or socket.

The wheel also owns `ae.kctl.profile_paths`, `ae.kctl.profile_state`,
`ae.kctl.profile_supervisor`, `ae.kctl.profile_engine`,
`ae.kctl.profile_bootstrap`, `ae.kctl.profile_component_runner`, and
`ae.kctl.profile_backend`, plus `ae.kctl.profile_host_plan`,
`ae.kctl.profile_host_transaction`, `ae.kctl.profile_host_helper`, and
`ae.kctl.profile_node_adapter`. The strict
installed-artifact gate imports them outside the checkout and exercises a
synthetic state-machine run only beneath a disposable owner-private XDG root.
That smoke verifies read-only absent-state inspection, canonical state,
cleanup completion, and module provenance without launching a process,
selecting a backend, contacting a socket, or touching the invoking user's
real HOME/XDG directories. The installed smoke constructs the deterministic
local plan with a synthetic injected capability but never probes an engine,
opens a socket, launches a process, or activates a runtime.

Canonical `local`, `core`, `edge-gateway`, and `node` expose installed portable
lifecycle source paths as `source-validated/unqualified`; this posture is not
live Podman, Docker, gateway transport, or node qualification. The installed
artifact smoke creates and reloads synthetic owner-private gateway/node
bootstraps, verifies their secret-free public projection and private drift
fence, and constructs injected non-service plans outside the checkout. It
does not invoke an engine, transport, process, socket, service, or real user
credential. Development/preview compatibility actions and lab/example helpers
deliberately use repository scripts. Those repository paths are not copied
into the runtime wheel.

The off-checkout gate also creates synthetic owner-private strict-CRI intent
and host-fact files, runs the installed host-plan command with both exact
digests, and proves the two files and their directory are unchanged. Python
socket operations remain disabled. A second synthetic smoke reconstructs a
node plan, drives prepare and exact rollback through bounded anonymous pipes
and an injected no-op helper boundary, and resolves the installed node adapter
against the exact applied receipt. It starts no helper process, opens no CRI
endpoint, changes no host target, invokes no service, and makes no claim that
the synthetic facts describe a live host.

## Version posture

The `v0.1.7` tag and changelog establish the latest stable lineage. `0.1.8a1`
introduced the `k1s-core` distribution transition, and `1.0.0rc1` is the
current PEP 440 release-candidate source identity. The alpha established the
public compatibility contract in `public-contracts.md`; RC1 does not make
experimental surfaces stable or claim broad production readiness.

The immutable local annotated `v1.0.0rc1` tag identifies the exact reviewed
source checkpoint. That local tag is not evidence of package-index
publication, deployment, live qualification, or stable promotion.

The accepted implementation baseline completed source and synthetic checks for
the isolated Q1 capsule, conductor, teardown, and replay contracts before this
version transition. It did not complete live disposable-guest CRI/containerd,
provider, deployment, or appliance qualification. The `1.0.0rc1` artifacts are
rebuilt from their own immutable commit and must pass deterministic two-lane,
strict installed-artifact, and publication review gates. The K1 review at
`c9178a5235b695784ed839f31901d476afdd6a98` qualifies the WorkerBee v1 wire and
pure same-runtime consumer fixture; live W4 acquisition/channel binding and
combined K1s/WorkerBee qualification remain required before publication or
stable promotion.

The canonical runtime version is the literal `ae.__version__` in
`src/ae/__init__.py`. Setuptools reads that literal as dynamic distribution
metadata. The `ae.kctl` compatibility module, controller OpenAPI, API-shim
OpenAPI, committed OpenAPI snapshots, and release checks must match it; no
second version literal belongs in `pyproject.toml`.

## Artifact promotion

All release artifacts are constructed by the committed
`scripts/build_python_artifacts.py` helper from an exact full commit and tree.
The helper reads blob bytes and modes from Git objects, never from checkout
permissions: Git `100644` becomes `0644`, Git `100755` becomes `0755`, and
synthesized directories become `0755`. Unsupported modes, symlinks, gitlinks,
unsafe or colliding paths, protected local paths, missing objects, and any
workspace-mode influence fail closed.

The helper builds without isolation or indexes under the fixed
`SOURCE_DATE_EPOCH=1793664000`. It canonicalizes both artifacts rather than
trusting backend container metadata. Wheel entries have stable ordering,
timestamps, regular-file modes, ZIP attributes and compression, and the helper
rebuilds complete `RECORD` ownership. Source-distribution entries have stable
ordering, Git-derived modes, timestamps, ownership, PAX policy, and gzip
headers. Inputs are immutable and normalized outputs are atomically created as
new files.

Every verified bundle contains the wheel, normalized source distribution,
canonical source/wheel/sdist member manifests, `artifact-manifest.json`, and
`SHA256SUMS`. The manifest binds the exact commit/tree, toolchain, release lock,
reviewed 24-wheel closure, artifact identities, member manifests, and validator
results without retaining checkout paths, hostnames, temporary names, or an
environment dump. Construction and verification deny network sockets and use
`PIP_NO_INDEX=1` with `PIP_CONFIG_FILE=/dev/null`.

CI builds from the same commit under umasks `022` and `077` and requires every
bundle byte to match. CI has no publication authority. The TestPyPI workflow
uses the same helper once, independently verifies and attests the complete
bundle, and publishes only its `dist/` directory with OIDC trusted publishing.
It does not contain an alternate `python -m build`, mode-repair, normalizer, or
manifest-construction path. The
production workflow accepts the successful TestPyPI workflow run ID and source
commit, downloads the named artifact from that run, verifies the workflow
identity, commit, file set, and every SHA256 value, and publishes those exact
bytes with OIDC. Production never rebuilds or resolves a package from an
index.

The canonicalization checkpoint intentionally establishes a new artifact
container identity before public alpha. Earlier RC evidence hashes remain
review evidence only and are not publication inputs. Distribution identity,
version, Python payload, resources, and console contracts are unchanged.

Local acceptance repeats the controlled build, checks byte-for-byte equality,
runs Twine, archive namespace, and public-marker audits, and installs
exclusively from the hash-locked `requirements-release.lock` wheelhouse with
package indexes and Python socket access disabled. The installed-artifact gate
runs from outside the checkout, checks package resources and CRI protobufs,
exercises every compatibility console with `--help`, and runs an `ae plan`
smoke with only the stub runtime selected. GitHub attaches build-provenance
attestations to the exact wheel, source distribution, and `SHA256SUMS` file;
the production workflow verifies those attestations before its OIDC publish
step.
