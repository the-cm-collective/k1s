# Python Distribution and Compatibility

The installable Python distribution is `k1s-core` beginning with `0.1.8a1`.
This distribution identity matches the K1s core runtime consumed by local
WorkerBee wheelhouses. The rename does not rename the runtime's established
Python or command surfaces.

## Preserved compatibility surfaces

- Python imports remain under `ae`, including `import ae` and all
  `ae.runtime.cri` modules.
- The primary operator command remains `k1s`.
- `k1s profile start|status|stop|restart` provides one stable lifecycle
  contract for the `core`, `edge`, `core-edge`, and `edge-core` profiles. It
  delegates to the existing profile runner and keeps per-profile PID/log state
  separate from workload reconciliation state.
- Existing commands `ae`, `ae-rotate-certs`, `ae-node`, `ae-gateway`, and
  `ae-worker-stub` remain installed by `k1s-core`.
- CRI protobuf modules remain under
  `ae.runtime.cri.api.runtime.v1`. The gogo descriptor keeps its canonical
  `github.com/gogo` protobuf identity internally, but no top-level `github`
  Python package is shipped.

The previous distribution requirement `ae` is not an alias for `k1s-core`.
Dependency declarations and installation commands must migrate to
`k1s-core`. Runtime imports and scripts do not require migration.

## Version posture

K1s remains pre-1.0. The `v0.1.7` tag and changelog establish the latest
stable lineage, while the repository's versioning policy requires PEP 440
pre-release identifiers for candidates. `0.1.8a1` is therefore the next
candidate for the distribution transition. `1.0.0a1` is intentionally not
used because the repository does not yet declare or validate a 1.0
compatibility contract.

The canonical runtime version is `ae.__version__`. Distribution metadata, the
`ae.kctl` compatibility module, controller OpenAPI, API-shim OpenAPI, and
committed OpenAPI snapshots must match it.

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
runs Twine and archive namespace audits, and installs exclusively from a local
wheelhouse with package indexes disabled.
