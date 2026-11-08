# Python Distribution and Compatibility

The installable Python distribution is `k1s-core` beginning with `0.1.8a1`.
This distribution identity matches the K1s core runtime consumed by local
WorkerBee wheelhouses. The rename does not rename the runtime's established
Python or command surfaces.

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

K1s remains pre-1.0. The `v0.1.7` tag and changelog establish the latest
stable lineage, while the repository's versioning policy requires PEP 440
pre-release identifiers for candidates. `0.1.8a1` is therefore the next
candidate for the distribution transition. `1.0.0a1` is intentionally not
used because the repository does not yet declare or validate a 1.0
compatibility contract.

The canonical runtime version is the literal `ae.__version__` in
`src/ae/__init__.py`. Setuptools reads that literal as dynamic distribution
metadata. The `ae.kctl` compatibility module, controller OpenAPI, API-shim
OpenAPI, committed OpenAPI snapshots, and release checks must match it; no
second version literal belongs in `pyproject.toml`.

## Artifact promotion

The TestPyPI workflow builds one wheel and one source distribution under a
fixed `SOURCE_DATE_EPOCH`, normalizes sdist container timestamps and ownership,
validates their names and metadata, writes `SHA256SUMS`, and publishes that
bundle with OIDC trusted publishing. The
production workflow accepts the successful TestPyPI workflow run ID and source
commit, downloads the named artifact from that run, verifies the workflow
identity, commit, file set, and every SHA256 value, and publishes those exact
bytes with OIDC. Production never rebuilds or resolves a package from an
index.

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
