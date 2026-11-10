# Isolated K1s v1 qualification capsule

K-P1-Q1 is the only live qualification boundary for the installed portable
profiles and strict-CRI host contract. The committed capsule tooling remains
`source-validated/unqualified` until one exact run is independently accepted.
Building or importing the tooling, constructing a plan, or passing synthetic
tests is not a live-runtime claim.

The capsule never starts WorkerBee. It may produce the metadata-safe K1s-side
WorkerBee evidence operations, but W-P0-1 owns the later WorkerBee consumer and
transport boundary.

## Prerequisites

One run is allowed only when all of these immutable inputs are present:

- an exact qcow2 base image and SHA-256;
- a canonical guest manifest and SHA-256;
- the accepted `k1s-core` wheel and normalized sdist;
- the reviewed 24-wheel, hash-locked offline dependency closure;
- the qualification guest-helper script and SHA-256;
- exact `qemu-system-x86_64`, `qemu-img`, and seed-builder executable
  identities;
- a fresh ephemeral SSH public key supplied explicitly, never discovered from
  `HOME`;
- a loopback SSH port selected by an exact collision gate; and
- an explicit existing capsule parent that is canonical, absolute, free of
  symlink components, owned by the invoking user, mode `0700`, writable, and
  on a supported local filesystem with at least 64 GiB free.

The approved local operator target is
`/srv/storage/k1s/q1-qualification`. Creating or repairing that directory is a
separate host operation; QH0 does not do it. A private directory below `/tmp`
is compatible only when the operator selects it explicitly and it passes the
same owner, mode, path, and local-filesystem checks. `/tmp` itself is not an
acceptable parent.

The guest manifest is canonical JSON with API version
`k1s.dev/q1-capsule/v1`, kind `K1sQ1GuestManifest`, and schema version 1. It
binds the qcow2, OS release, kernel package and image, Python, containerd,
`runc`, `crictl`, rootless Podman, Docker, the five required CNI plugins and
configuration template, the local registry image, every offline OCI image,
the dependency lock and wheelhouse, and the guest helper. Mutable tags or a
version without the corresponding content digest are not authority. The host
pins the external manifest digest and the guest must contain the same bytes.

## Pure plan

`plan` validates every explicit regular-file and executable identity with
no-follow, owner, mode, link-count, size, stability, and digest checks. It
creates no run root, overlay, ISO, socket, process, service, or runtime state.
The result binds all inputs, the source commit/tree, limits, QEMU argv, network
posture, acceptance order, selected-parent device/inode/path identity, and
exact `<capsule-parent>/k1s-<run-id>` layout. Caller umask, current directory,
`HOME`, and a module-global temporary path are not authority.

Example shape:

```text
python3 scripts/qualification/kp1_q1_capsule.py plan \
  --run-id q1-0123456789abcdef0123456789abcdef \
  --capsule-parent /srv/storage/k1s/q1-qualification \
  --source-commit COMMIT --source-tree TREE \
  --base-image /absolute/base.qcow2 \
  --expect-base-image-sha256 sha256:... \
  --guest-manifest /absolute/q1-guest-manifest-v1.json \
  --expect-guest-manifest-sha256 sha256:... \
  --wheel /absolute/k1s_core-1.0.0a3-py3-none-any.whl \
  --expect-wheel-sha256 sha256:... \
  --sdist /absolute/k1s_core-1.0.0a3.tar.gz \
  --expect-sdist-sha256 sha256:... \
  --wheelhouse-manifest /absolute/wheelhouse-manifest.json \
  --expect-wheelhouse-manifest-sha256 sha256:... \
  --guest-helper /absolute/kp1_q1_guest_helper.py \
  --expect-guest-helper-sha256 sha256:... \
  --ssh-public-key /absolute/q1.pub \
  --expect-ssh-public-key-sha256 sha256:... \
  --qemu /usr/bin/qemu-system-x86_64 \
  --expect-qemu-sha256 sha256:... \
  --qemu-img /usr/bin/qemu-img \
  --expect-qemu-img-sha256 sha256:... \
  --seed-builder /usr/bin/genisoimage \
  --expect-seed-builder-sha256 sha256:... \
  --ssh-host-port PORT \
  --output /absolute/private/plan.json
```

The output file's parent must already be an owner-only mode-`0700`
directory. The plan remains private because it contains exact host input
paths. Public evidence contains identities and outcomes, not those paths.

## QH0 and QH1 operator flow

QH0 produces the pure plan above. QH1 then performs a read-only host
preflight against exactly that plan:

```text
python3 scripts/qualification/kp1_q1_capsule.py preflight \
  --plan /absolute/private/plan.json \
  --expect-plan-sha256 sha256:... \
  --output /absolute/private/host-preflight.json
```

The canonical preflight receipt binds the run ID and plan digest, selected
parent identity and free space, four-vCPU and 8-GiB host capacity, exact QEMU
tool identities, the symbolic group name `kvm` and its resolved GID, the full
`/dev/kvm` device/inode/owner/group/mode/major/minor identity, invoking UID and
current read/write access, and loopback SSH-port availability. The port check
is a point-in-time observation, never a reservation. The later execution
boundary must recheck and bind the port immediately before QEMU launch or fail
closed. Receipts expire after five minutes.

`gate-a-inspect` is a read-only verifier for an already produced Gate A
attestation. It validates the plan, preflight, attestation, current parent,
symbolic group, and current KVM identity and access, and returns only a
sanitized `valid-current` result. It does not grant access or modify the host:

```text
python3 scripts/qualification/kp1_q1_capsule.py gate-a-inspect \
  --plan /absolute/private/plan.json \
  --expect-plan-sha256 sha256:... \
  --host-preflight /absolute/private/host-preflight.json \
  --expect-host-preflight-sha256 sha256:... \
  --gate-a-attestation /absolute/private/gate-a.json \
  --expect-gate-a-sha256 sha256:...
```

On the currently reviewed host, `/dev/kvm` has group GID `42427` while the
symbolic `kvm` group resolves to GID `108`. QH1 must report
`kvm-group-mismatch` and Gate A must fail closed until a separately reviewed
host repair makes those identities agree. These source changes do not repair
the mismatch and do not qualify K1s `1.0.0a3`.

## Isolation and limits

The future `run` boundary creates exactly one owner-private run root with the
subdirectories `input`, `vm`, `guest`, `evidence`, and `cleanup`, all mode
`0700`. It uses one read-only base and one 24 GiB qcow2 overlay. The VM has four
vCPUs, 8 GiB RAM, a 45-minute wall clock, bounded command output, bounded
retained evidence, and one exact QMP socket, pidfile, and serial log.

QEMU uses KVM, `q35`, `-cpu host`, virtio block/network/RNG, a restricted
user-mode network, and one loopback-only SSH forward. The argv contains no
tap, bridge, host route, firewall rule, libvirt object, 9p/virtfs mount, shared
folder, source checkout, or `HOME` path. QEMU's restricted user network is the
host-side default-deny boundary. A live capsule must additionally establish
the sealed guest policy and prove both DNS and general egress fail before the
offline inputs are transferred.

The capsule never repairs a missing prerequisite. In particular, it does not
create `/dev/kvm`, load modules, change groups, grant an ACL, fetch an image,
install host packages, select a different accelerator, or fall back to TCG.

## Two separate paired approvals

### Gate A: host KVM access

Before `run`, a trusted-host operator attests an existing character device
with major/minor `10:232`, `root:kvm`, mode `0660`, a stable inode/device
identity, the exact selected-parent and plan identities, a bounded timestamp,
and access for the exact invoking UID. The attestation includes its own
canonical digest and expires after five minutes. A stale, replayed, tampered,
cross-plan, cross-parent, or numerically matching but symbolically different
group attestation fails closed. If the exact user lacks access, the
only separately approved host mutation is one temporary `rw` ACL entry on that
already-attested device. Rollback removes only the newly added ACL entry after
the same identity is revalidated. The capsule never creates or removes the
device and never changes its owner or mode. QH0/QH1 implement no grant, apply,
or rollback command; that remains a separately paired privileged action.

`run` requires the canonical unexpired host-preflight receipt and Gate A
attestation, both exact file digests, the exact plan digest, and the literal
confirmation `RUN_ID:PLAN_DIGEST`. It revalidates the selected parent, every
input, the current KVM identity and access, and the SSH port before invoking
the already plan-bound QEMU command. A failed recheck does not select another
port or engine.

Teardown is also digest- and confirmation-bound. It accepts only the exact
canonical `guest/run.json`, its file digest, and
`RUN_ID:RUN_RECEIPT_DIGEST`, after the recorded QEMU identity is absent.
`verify-clean` accepts the exact canonical cleanup receipt and its file digest;
both commands require the same explicit revalidated capsule parent plus the
run ID. Neither command performs ambient process or filesystem discovery or
uses a hidden default root.

### Gate B: guest strict-CRI helper

Gate B occurs only after boot and containment checks. Its canonical
attestation binds the guest image, QEMU process/argv/executable, helper and
guest-Python bytes, exact request/response FD topology, transaction and target
IDs, plan digest, action count, explicit socket-access UID/GID, and exact
`setfacl`/`getfacl` executable identities.

The helper runs inside the disposable guest with a clean environment, explicit
interpreter and script, `cwd=/`, `umask 077`, no shell, no ambient authority,
no network, and only anonymous protocol FDs. For every request the installed
helper engine reloads canonical transaction authorities, rebuilds the host
plan, and derives the selected action. The qualification boundary accepts only
`validate`, `verify`, `copy-regular`, `write-canonical`, and
`grant-exact-acl`. It maintains descriptor-relative private backups, refuses
drift, supports exact replay, and restores the private pre-state during
receipt-authorized rollback. Public errors are generic and contain no private
path, ACL, byte content, or receipt detail.

Gate A grants only KVM access. Gate B grants only one exact in-guest
transaction. Neither approval implies the other.

## QH2 sealed input bundle and Gate B bridge

QH2 is a source-and-synthetic-test checkpoint. It neither creates a guest
image nor installs or starts the Gate B endpoint. The bundle builder and
validator are dependency-free and are the only construction boundary for the
candidate guest payload. The candidate is exactly `k1s-core` `1.0.0a3`; a
different distribution or version fails closed.

The uncompressed canonical USTAR bundle has one leading `manifest.json` and
exactly 31 payload members in UTF-8 byte order:

- the accepted `k1s_core-1.0.0a3-py3-none-any.whl` and normalized
  `k1s_core-1.0.0a3.tar.gz`;
- `requirements-release.lock`, the canonical wheelhouse manifest, and the
  exact 24 dependency wheels;
- `kp1_q1_guest_helper.py`; and
- `validate_installed_python_artifact.py`; and
- the same dependency-free `kp1_q1_bundle.py` validator used by the guest.

The canonical manifest binds every member path, role, mode, size, and SHA-256;
the source commit/tree, run ID, capsule plan digest, fixed source epoch, exact
offline-install posture, and the Gate B endpoint. Regular members are either
mode `0644` or `0755`; uid/gid are zero in the archive; timestamps are fixed;
and uname/gname and PAX metadata are empty. Symlinks, hardlinks, special
members, duplicate or case-fold-colliding names, traversal, protected names,
unsupported fields or versions, input replacement, and noncanonical archive
bytes are rejected. The bundle ID is the SHA-256 of the domain-separated
canonical manifest with `bundleId` omitted. A consumed bundle ID cannot be
accepted by another transaction.

Construction takes one owner-only mode-`0600` canonical JSON specification.
Each `inputs` row contains exactly `sourcePath`, `bundlePath`, `role`, and
`expectedSha256`; no source path is written to the bundle. The specification
also contains `runId`, `planDigest`, `sourceCommit`, `sourceTree`, and an
`endpoint` object with the expected Python, `setfacl`, and `getfacl` digests.
All referenced inputs must be canonical absolute, single-link regular files
owned by the invoking user, mode `0644` or `0755`, and stable during reading.
The absent output is created mode `0600` below an existing owner-only mode
`0700` directory:

```text
python3 scripts/qualification/kp1_q1_bundle.py build \
  --spec /absolute/private/q1-bundle-spec.json \
  --expect-spec-sha256 sha256:... \
  --output /absolute/private/k1s-q1-input.bundle

python3 scripts/qualification/kp1_q1_bundle.py verify \
  --bundle /absolute/private/k1s-q1-input.bundle \
  --expect-bundle-sha256 sha256:... \
  --run-id q1-0123456789abcdef0123456789abcdef \
  --plan-digest sha256:... \
  --bundle-id sha256:...
```

The capsule `run` command additionally requires
`--input-bundle`, `--expect-input-bundle-sha256`, and `--expect-bundle-id`.
It validates the bundle before creating the run layout, copies exact bytes to
`input/k1s-q1-input.bundle`, validates the copy again, and includes only that
private copy in the seed input. Any bundle drift precedes and prevents the
first external command.

```text
python3 scripts/qualification/kp1_q1_capsule.py run \
  --plan /absolute/private/plan.json \
  --expect-plan-sha256 sha256:... \
  --expect-plan-digest sha256:... \
  --confirm RUN_ID:PLAN_DIGEST \
  --host-preflight /absolute/private/host-preflight.json \
  --expect-host-preflight-sha256 sha256:... \
  --gate-a-attestation /absolute/private/gate-a.json \
  --expect-gate-a-sha256 sha256:... \
  --input-bundle /absolute/private/k1s-q1-input.bundle \
  --expect-input-bundle-sha256 sha256:... \
  --expect-bundle-id sha256:...
```

The Gate B endpoint descriptor is itself digest-bound. It names the
single-transaction nonpersistent service `k1s-q1-gate-b`, uid/gid zero, the
exact helper and bundle-validator paths and digests, `/usr/bin/python3.11`,
`setfacl`, and `getfacl` digests, request FD 3, response FD 4, `cwd=/`, umask
`0077`, the exact clean environment, 30-second total deadline, 64-request and
256-KiB protocol ceilings, and no shell or ambient sudo. The canonical
five-minute attestation additionally binds the bundle and manifest identities,
run and plan, transaction and target, access uid/gid, request count, and the
exact rollback receipt digest. Unknown, expired, divergent, replayed, or
cross-bundle authority fails closed.

The endpoint descriptor is a protocol and compatibility identity, not proof of
peer-process identity. Anonymous pipes alone do not authenticate the process at
their other end. The later paired live gate must independently bind the guest
image, installed module bytes, root launch authority, process identity, and FD
topology before sending any request.

Inside a future disposable guest, the helper independently revalidates the
private mode-`0600` bundle and Gate B attestation, its own bytes, the validator,
Python, and ACL tools. It requires root, `/`, umask `077`, the exact environment,
and two anonymous pipe descriptors. Framing reads and writes share the single
deadline and fixed size ceiling. Every request must match the one transaction,
target, and plan; rollback additionally must carry the attested receipt digest.
Public failure output is the single generic failed-closed message.

The future root launcher must create anonymous pipes as FDs 3 and 4, set the
exact environment and umask before exec, and invoke only the pinned
interpreter and helper without a shell:

```text
/usr/bin/python3.11 /opt/k1s-q1/kp1_q1_guest_helper.py \
  --request-fd 3 --response-fd 4 \
  --input-bundle /opt/k1s-q1/input/k1s-q1-input.bundle \
  --expect-input-bundle-sha256 sha256:... \
  --expect-bundle-id sha256:... \
  --run-id RUN_ID --plan-digest sha256:... \
  --gate-b-attestation /run/k1s-q1/private/gate-b.json \
  --expect-gate-b-sha256 sha256:... \
  --acceptance-receipt \
    /run/k1s-q1/private/acceptance/BUNDLE_ID_HEX.json \
  --setfacl /usr/bin/setfacl --getfacl /usr/bin/getfacl
```

Those paths are fixed by the sealed endpoint and are illustrative only until
the independently reviewed live launcher has installed the exact bytes and
proven their owner, mode, digest, process, and FD identities.

The acceptance directory must already exist as a root-owned mode-`0700`
directory. The receipt filename is derived only from the bundle ID and is
therefore the same for every attempted transaction using those exact bundle
bytes. After process context, bundle, attestation, and every executable
identity pass—but before the first request is read—the helper atomically
creates one canonical root-owned mode-`0600` `accepted-once` receipt. It uses
an exclusive no-follow pending file and an exclusive hard-link publication so
the final path is never overwritten. The receipt binds the bundle, bundle
manifest, run, plan, and transaction. An existing final path of any kind,
malformed or divergent receipt, unsafe parent, owner/mode/link/device drift, or
second invocation fails closed. Only a pending file created by the failing
invocation may be removed; a final receipt is retained as replay authority.

The offline installation contract is fixed to `PIP_CONFIG_FILE=/dev/null`,
`PIP_NO_INDEX=1`, `--no-index`, `--require-hashes`, and the exact 24-wheel
closure. QH2 performs no install, service creation, privilege transition,
network access, guest-image construction, ISO creation, or QEMU action. A
separate paired live authorization must install and invoke the exact endpoint
inside the disposable guest, and only independent acceptance of the entire Q1
matrix may change the release posture from `source-validated/unqualified`.

## QH3 ordered conductor contract

QH3 adds the dependency-free source-side conductor contract in
`scripts/qualification/kp1_q1_conductor.py`. It is still a synthetic
checkpoint: the module does not implement SSH, QMP, host-process discovery,
privilege changes, guest creation, or teardown. A later live capsule must
supply separately reviewed adapters for those transports.

The conductor fixes all 14 acceptance stages, transport classes, targets,
argument-vector identities, per-stage limits, and total deadline. It accepts
no shell command text and offers no fallback transport. Before the first
adapter call it verifies the exact bundle bytes, then binds the run,
transaction, plan, bundle, source commit, source tree, stage schema, fixed
45-minute total deadline, and replay claim. Every adapter request carries that
complete authority plus one monotonic deadline and the fixed output/evidence
ceilings. A missing, unreadable, or already divergent bundle prevents both the
replay claim and every adapter call.

Each adapter response must return the complete run, transaction, plan, bundle,
source commit, and source tree identity, an integer exit code, timeout flag,
bounded byte streams, and typed evidence descriptors. Raw output and adapter
exceptions never enter the public receipt; receipts contain only sizes,
SHA-256 identities, fixed classifications, and bounded metadata. The bundle
identity is re-read before and after every adapter call; pre-call drift blocks
that call. Malformed responses, identity or bundle drift, timeout, nonzero
exit, excessive output, excessive evidence, adapter error, and total-deadline
expiry fail the current stage closed.

The first failed stage stops adapter execution. Every later stage receives a
canonical `not-run` receipt, and the top-level receipt records the failed stage
and schedules teardown. A passed stage cannot appear after a failure. Stage
and top-level receipts are canonical JSON with independent SHA-256 digests;
validation rejects unknown fields, reordering, skipped stages, changed command
identities, altered authority, inconsistent status, oversized metadata, or
receipt tampering. The replay ledger prevents a second conductor invocation
for the same authority. The fixed total deadline participates in the receipt
ID and top-level receipt, passed stages must carry the authority's exact bundle
digest, and `not-run` stages must carry canonical empty output descriptors.

The nonmutating schema command exposes the closed contract for review:

```text
python3 scripts/qualification/kp1_q1_conductor.py schema
```

QH3 does not make `source-validated/unqualified` live-qualified. The concrete
SSH/QMP/process adapters, exact process teardown, retained-evidence sealing,
independent replay, and zero-residue proof remain later paired checkpoints.

## QH4 exact shutdown and evidence replay contract

QH4 adds the dependency-free source-side contract in
`scripts/qualification/kp1_q1_teardown.py`. It remains synthetic-only: the
module defines typed QMP, process-observer, clock, inventory, and filesystem
boundaries but provides no socket, subprocess, signal, service, privilege, or
runtime implementation. Tests supply fakes and owner-private temporary trees.

The shutdown model is exact and closed. It first revalidates the recorded
process by PID, start-time ticks, canonical executable, and the complete argv.
Only then may the injected QMP adapter receive the ordered commands
`query-status`, `system_powerdown`, bounded `query-status` polls ending in
`shutdown`, and `quit`. Every request binds the complete QH3 authority, a
domain-separated digest of the private process identity, a monotonic deadline,
the fixed 64-KiB response ceiling, and an exact sequence number. The contract
never signals or discovers a process by name, pattern, port, or executable.
After QMP completion, bounded exact-PID observations must prove absence; a
reused PID or any start-time, executable, or argv substitution fails closed.
Public receipts contain only the process-identity digest and bounded QMP
payload digests and sizes, never the PID, executable, argv, QMP path, or raw
output.

Host inventory snapshots are canonical sorted sets of typed opaque SHA-256
identities. The allowed kinds are `file`, `listener`, `network-interface`,
`process`, and `runtime-object`. Before and after inventories must be exactly
equal; duplicates, unknown kinds, reordered rows, changed identities, or more
than 4096 entries fail. The zero-residue receipt additionally requires the
exact `k1s-RUN_ID` root to be absent beneath the revalidated owner-private
capsule parent. It binds only domain-separated parent, run-root, retained
evidence-root, process, shutdown, and inventory-comparison digests; private host
paths are not rendered.

The retained evidence directory is an existing owner-owned mode-`0700`
directory and initially contains exactly these owner-owned, single-link,
mode-`0600` canonical JSON files:

1. `conductor-receipt.json`
2. `inventory-after.json`
3. `inventory-before.json`
4. `inventory-comparison.json`
5. `shutdown-receipt.json`
6. `zero-residue.json`

Sealing independently validates every relation, reconstructs lexical member
rows, and exclusively creates `Q1-EVIDENCE-MANIFEST.json`, `SHA256SUMS`, and
`Q1-EVIDENCE-SEAL.json`. It refuses overwrite, links, special files, foreign
owner/device/mode, noncanonical or duplicate-key JSON, unexpected members,
oversize input, authority drift, or inconsistent receipts. Replay enumerates
the exact nine-file directory without following links, reconstructs every
member row and checksum from actual bytes, revalidates the conductor,
shutdown, inventory, and zero-residue relations, and optionally requires the
exact seal-file digest. A recomputed checksum file cannot make false manifest
or receipt claims acceptable.

The schema, seal, and independent replay CLI surfaces are:

```text
python3 scripts/qualification/kp1_q1_teardown.py schema

python3 scripts/qualification/kp1_q1_teardown.py seal \
  --root /absolute/private/retained-evidence \
  --authority /absolute/private/run-authority.json \
  --expect-authority-sha256 sha256:...

python3 scripts/qualification/kp1_q1_teardown.py replay \
  --root /absolute/private/retained-evidence \
  --authority /absolute/private/run-authority.json \
  --expect-authority-sha256 sha256:... \
  --expect-seal-sha256 sha256:...
```

These source contracts do not perform QMP, inspect a host process, remove a
capsule, or qualify the release. A later paired live gate must provide and
independently review the exact QMP and process observers, preserve the sealed
bundle, move retained evidence without substitution, and demonstrate teardown
and host-inventory equality inside the disposable egress-denied guest before
any `live-qualified` claim.

## QH5-B B0 deterministic guest-image source contract

QH5-B B0 adds two independent, dependency-free source contracts. It does not
download a base image, install packages, invoke QEMU/KVM, create a qcow2 image,
start a guest, or promote the release posture.

`scripts/qualification/kp1_q1_guest_image.py` validates an exact private build
specification and emits a canonical plan for two independent lanes:

1. `lane-a` uses umask `0022`;
2. `lane-b` uses umask `0077`.

Each lane must execute the same closed stage order:

1. `authenticate-base`;
2. `validate-offline-bom`;
3. `stage-inputs`;
4. `create-image`;
5. `provision-offline`;
6. `finalize-image`;
7. `extract-inventory`; and
8. `validate-guest-manifest`.

The plan accepts only a dated immutable Canonical Jammy release URL, exact
accepted-candidate source commit and tree, canonical offline BOM, retained B1
and B2 plan/seal authorities, digest-bound qualification sources and local
tools, an owner-private mode-`0700` output parent, fixed stage and total
deadlines, and absent lane output paths. It records file device, inode, owner,
mode, size, and SHA-256 so a later adapter must detect substitution before
execution. The module defines a typed adapter boundary and canonical receipt,
and B3 supplies the separately bound live image-builder adapter. Synthetic
tests prove stage ordering,
identity binding, bounded output, fail-closed timeout/error handling, and exact
lane artifact equality. The historical generic Packer lane is not accepted as
Q1 authority by this contract.

The nonmutating plan and verification surfaces are:

```text
python3 scripts/qualification/kp1_q1_guest_image.py plan \
  --spec /absolute/private/q1-guest-image-spec.json \
  --expect-spec-sha256 sha256:... \
  --output /absolute/private/q1-guest-image-plan.json

python3 scripts/qualification/kp1_q1_guest_image.py verify-plan \
  --plan /absolute/private/q1-guest-image-plan.json \
  --expect-plan-sha256 sha256:...
```

`scripts/qualification/kp1_q1_guest_inventory.py` is independent of the image
builder. A later guest adapter must return bounded regular-file observations
and exact argv results; no shell command text is accepted. The extractor reads
fixed OS, kernel, Python, runtime, CNI, and offline-evidence paths, validates
their modes and identities, executes only bounded version/package queries, and
reconstructs the complete `k1s.dev/q1-capsule/v1` guest manifest. That manifest
must pass the capsule's existing closed validator before the extractor emits a
canonical inventory receipt. Builder output alone therefore cannot assert its
own guest contents.

B0 proves source contracts and synthetic adapter behavior only. B1 adds the
separately reviewed base-acquisition adapter below. The live builder is B3:
later QH5-B checkpoints must build both lanes,
compare exact image and manifest identities, boot the accepted artifact under
the sealed Q1 capsule, replay inventory independently, complete the ordered
acceptance matrix, and prove teardown and host-inventory equality. Until all
those gates pass, the posture remains `source-validated/unqualified`.

## QH5-B B1 authenticated base acquisition

`scripts/qualification/kp1_q1_base_acquisition.py` is the only B1 network
boundary. Its canonical specification binds the QH5-B transaction by file
digest, the exact K1s source commit/tree, and the exact local `curl`, `gpgv`,
and `qemu-img` executable identities. It accepts one dated Canonical Jammy
release directory and creates one absent owner-private root directly below
`/srv/storage/k1s`.

The adapter downloads exactly `SHA256SUMS`, `SHA256SUMS.gpg`, the requested
package manifest, and the requested qcow2. Curl is restricted to HTTPS with a
clean environment, fixed time and size ceilings, and no shell. Before the
image is accepted, `gpgv` must authenticate the checksum set with the pinned
Canonical keyring and the transaction's exact signing fingerprint. Both local
artifacts must match the signed checksum rows and transaction digests. The
only image operation is `qemu-img info --output=json`; B1 requires qcow2, no
backing file, and bounded actual and virtual sizes. It never invokes a QEMU
system binary or `/dev/kvm`.

The operator first produces and reviews a canonical plan:

```text
python3 scripts/qualification/kp1_q1_base_acquisition.py plan \
  --spec /absolute/private/b1-spec.json \
  --expect-spec-sha256 sha256:... \
  --output /absolute/private/b1-plan.json
```

Acquisition requires the exact plan file digest and a confirmation containing
the plan's canonical digest:

```text
python3 scripts/qualification/kp1_q1_base_acquisition.py acquire \
  --plan /absolute/private/b1-plan.json \
  --expect-plan-sha256 sha256:... \
  --confirm ACQUIRE-Q1-BASE:sha256:...
```

Success retains four authenticated upstream files plus a canonical receipt,
local checksum set, and evidence seal, all mode `0400` under a mode-`0700`
root. Independent replay re-enumerates that exact seven-file set, rehashes the
actual bytes, validates every receipt/seal relation, and can require the exact
seal-file digest:

```text
python3 scripts/qualification/kp1_q1_base_acquisition.py verify-root \
  --plan /absolute/private/b1-plan.json \
  --expect-plan-sha256 sha256:... \
  --expect-seal-file-sha256 sha256:...
```

On failure, B1 first writes one sibling failure receipt, then removes only its
new root if every member belongs to the closed B1 filename set and is an
owner-owned, single-link regular file on the same filesystem. An unexpected
entry, link, device, owner, mode, or filesystem identity blocks cleanup rather
than broadening it. B1 changes no source checkout, host package, permission,
service, runtime, release endpoint, or publication state and cannot promote
the release beyond `source-validated/unqualified`.

## QH5-B B2 closed guest bill of materials

QH5-B B2 closes the complete offline input set consumed by the B0 image
builder. It still does not install a host package, start a runtime, build or
boot a guest, invoke QEMU/KVM, mount source or `HOME`, publish an artifact, or
advance the release posture.

`scripts/qualification/kp1_q1_guest_inputs.py` defines the only B2 network
boundary. Its canonical plan binds the exact source commit/tree, accepted
QH5-A wheel, sdist, 24-wheel closure, requirement lock and completion receipt,
the B1 base receipt and package manifest, the qualification helper, all local
tool identities, one absent owner-private root below `/srv/storage/k1s`, and
these external authorities:

- Ubuntu snapshot `20261107T000000Z`, with exact signed `jammy`,
  `jammy-updates`, and `jammy-security` `InRelease` bytes and the pinned Ubuntu
  archive signer;
- the exact signed deadsnakes Jammy `InRelease` needed for CPython `3.11.18`,
  with its pinned PPA signer and keyring;
- exact primary versions for the GA KVM kernel, containerd, `runc`, Docker,
  rootless Podman, CNI plugins, and Python; the `acl` utilities required by the
  digest-bound strict-CRI grant/rollback helper; plus an isolated resolver
  closure;
- cri-tools `1.30.1` with the vendor-published SHA-256; and
- nine source tags used only for discovery, each paired with an expected
  `linux/amd64` manifest digest: registry, pause, busybox, etcd, NATS,
  PostgreSQL, Envoy, rathole, and Caddy.

APT runs with an empty status database, private cache/list directories, no
ambient source parts, no recommendations, no host install, and only the four
explicit source rows. B2 retains the authenticated `InRelease` and package
index bytes, both keyrings, every exact `.deb`, and a canonical package ledger.
The ledger independently matches each downloaded package SHA-256 against the
authenticated APT metadata. Extracted package files are inspection inputs only:
they establish the expected kernel, OS release, Python, runtime, and five CNI
binary identities without changing the host.

The internal OCI distribution client accepts HTTPS only, handles anonymous
Bearer challenges, requires exactly one `linux/amd64` platform descriptor,
verifies every manifest/config/layer digest and size, and emits deterministic
USTAR OCI layouts. It never imports into Docker, Podman, containerd, or a host
registry. Mutable tags are not retained as authority; the final selected
manifest digest and archive SHA-256 are.

The operator first writes and reviews a canonical plan:

```text
python3 scripts/qualification/kp1_q1_guest_inputs.py plan \
  --spec /absolute/private/b2-spec.json \
  --expect-spec-sha256 sha256:... \
  --output /absolute/private/b2-plan.json
```

Acquisition requires the plan file digest and its canonical plan digest in the
explicit confirmation:

```text
python3 scripts/qualification/kp1_q1_guest_inputs.py acquire \
  --plan /absolute/private/b2-plan.json \
  --expect-plan-sha256 sha256:... \
  --confirm ACQUIRE-Q1-BOM:sha256:...
```

`scripts/qualification/kp1_q1_guest_bom.py` then enumerates only the declared
regular files beneath the private staging root. It refuses links, special
files, protected dotenv names, duplicate paths/roles, missing authorities,
guest-to-payload digest drift, and unexpected modes. It emits a deterministic,
uncompressed POSIX PAX payload with fixed owner, group, mode, timestamp, member
order, and an embedded canonical member manifest. PAX is required because one
accepted wheel has a standards-required basename longer than USTAR's 100-byte
name field. Only a `path` header equal to the validated member path is allowed
when the complete member name exceeds 100 bytes; all other PAX metadata is
rejected.

B2's gate uses two paths:

1. the existing B0 `validate_bom()` schema and guest-manifest validator check
   the separately retained canonical BOM; and
2. the independent payload replay enumerates the tar, rehashes each member,
   validates fixed PAX metadata, reads the embedded manifest, and reconstructs
   the BOM from the retained payload bytes.

Both paths must produce the same canonical BOM digest. Independent retained
root replay also compares the staged source bytes, payload, member manifest,
receipt, local checksum set, and evidence seal:

```text
python3 scripts/qualification/kp1_q1_guest_inputs.py verify-root \
  --plan /absolute/private/b2-plan.json \
  --expect-plan-sha256 sha256:... \
  --expect-seal-file-sha256 sha256:...
```

Success closes B2 only. B3 remains a separate reviewed boundary for two image
lanes and is never implied by acquisition or replay success.

## QH5-B B3 offline two-lane guest construction

QH5-B B3 adds `scripts/qualification/kp1_q1_guest_builder.py`. The accepted
candidate identity remains the commit and tree retained by B2; qualification
code added after candidate acceptance is independently digest-bound as B3 plan
input. This avoids relabeling a later tooling commit as the accepted K1s
artifact while ensuring that unrecorded builder code cannot produce evidence.

Before creating either lane, B3 independently replays the complete retained B1
root and complete retained B2 root against their exact plan and seal-file
digests. It also binds every imported qualification module, the live builder,
inventory module, guest helper bundle, QEMU system binary, `qemu-img`, Python,
filesystem-manifest policy module, `gpgv`, and ISO seed builder by regular file
identity and SHA-256. The live
adapter verifies that its executing source bytes equal those plan-bound copies
before creating a lane.

The host adapter has no shell-command surface. It invokes only exact argv lists
through a clean environment and creates two absent owner-private lane roots.
Each lane:

1. copies and expands the authenticated Jammy qcow2;
2. boots QEMU TCG with a fixed machine, CPU, virtual clock, UUID, memory, and
   CPU count, with `-nic none` and no KVM dependency;
3. exposes the retained B2 tar as one read-only raw virtio block device;
4. installs the exact resolver-closed package set, `crictl`, CNI plugins,
   accepted K1s wheel, and 24-wheel Python closure without an index or network
   device; the expected package count comes from the independently validated
   B2 payload manifest rather than a duplicated fixed count;
5. removes generated cloud identity, logs, SSH keys, random seed, Python
   bytecode, and snap device identity; normalizes Q1-owned modes independently
   of the lane umask; normalizes file timestamps; and powers off gracefully;
6. converts through raw into a normalized standalone qcow2; and
7. boots that image with QEMU snapshot writes and collects both the bounded
   runtime inventory and a complete persistent-filesystem manifest over the
   serial port.

The inventory boot has no SSH credential, host mount, source checkout, `HOME`,
socket, TAP, bridge, firewall, container runtime, or network device. Large
executables are represented by bounded streamed digest/size observations;
configuration and offline-evidence files remain bounded raw observations. The
host-side independent inventory module reconstructs and validates the closed
guest manifest. A separately bound filesystem-policy module validates every
persistent path by type, mode, uid/gid, content or link/device identity, and
extended-attribute digest. The root filesystem and the explicitly persistent
`/boot/efi` mount are included. The narrow policy excludes explicit runtime
mounts and reviewed first-boot state. Snapshot verification boots regenerate
the six SSH host-key files, both LXD Unix sockets (`lxd` and `lxd-user`), and
seven exact containerd, Podman, and Docker identity/state files before the
inventory script can quiesce those services. Those fifteen exact paths are
excluded while the rest of `/etc/ssh`, `/var/snap/lxd`, and the container
runtime state trees remains covered. The reusable image removes the seven
ambient runtime identities before shutdown. The policy never excludes broad
persistent roots such as `/etc`, `/opt`, `/usr`, or `/var`.

The final stage requires exact equality of the two canonical filesystem
manifest SHA-256 values, filesystem-policy SHA-256 values, and semantic guest
manifest SHA-256 values. Each lane retains its own exact qcow2 SHA-256 and
size, complete guest-manifest SHA-256, and inventory-receipt SHA-256. Physical
qcow2 bytes, ext4 allocation geometry, journal sequence, and time metadata are
not cross-lane equality fields because each lane is independently booted and
mutated. B3 still fails closed on any persistent path, ownership, mode,
content, link, device, xattr, guest semantic, or policy divergence.

Ordinary build stages retain a 20-minute deadline. The complete inventory
stage has a separately plan-bound 40-minute deadline because the isolated TCG
guest hashes every included regular file and extended attribute before it can
emit the canonical manifest. The two-hour whole-build deadline remains the
outer bound across both lanes.

The reviewed live invocation is explicit and plan-bound:

```text
python3 scripts/qualification/kp1_q1_guest_builder.py \
  --plan /absolute/private/q1-guest-image-plan.json \
  --expect-plan-sha256 sha256:... \
  --confirm BUILD-Q1-GUEST:sha256:... \
  --receipt /absolute/private/B3-BUILD-RECEIPT.json
```

On failure, B3 retains the owner-private lane and a bounded stage failure
record for review; it does not delete prior B1/B2 evidence or broaden cleanup.
B3 success remains `source-validated/unqualified`. It does not imply the B4
capsule/runtime acceptance matrix, release publication, tag, push, upload, or
artifact pruning.

## Required acceptance order

An accepted run records these stages in order:

1. boot and containment identity;
2. strict no-index installed-artifact validation;
3. pure profile/bootstrap/host contracts;
4. strict-CRI prepare, replay, node-adapter resolution, and rollback;
5. rootless-Podman core lifecycle;
6. separately selected Docker-parity core lifecycle;
7. gateway-only `edge-gateway` lifecycle;
8. explicit identity-v1 strict-CRI node lifecycle;
9. node lease generation, conflict, stale replacement, and old-owner fencing;
10. CRI security-context propagation and observed enforcement posture;
11. manifest apply, status, logs, restart, export, and delete;
12. all six metadata-safe K1s-side WorkerBee evidence projections;
13. exact component/coordinator/VM crash and recovery cases; and
14. strict rollback and zero-residue teardown.

Any skipped, reordered, ambiguous, truncated-without-classification, or
unsupported required stage makes the result unqualified.

The B4 live executor injects one digest-bound, root-owned guest stage runner
through cloud-init and then executes it as the unprivileged `q1` account. The
host connects only through the plan-selected loopback SSH forwarding port and
pins the guest host key before transferring the exact authority document and
sealed input bundle. QMP is accessed only through the exact owner-private Unix
socket in the run layout. QMP events may interleave command responses; the
bounded client ignores events while retaining buffered responses and rejects
unknown commands, malformed messages, errors, and oversized lines.

If a transport or stage adapter fails after launch, the live executor verifies
the exact PID, start time, executable, and argv identity, requests `quit` on the
bound QMP socket, proves process absence, and removes only the authority-bound
private capsule layout. It retains a metadata-safe failed-run cleanup receipt.
It never discovers or signals QEMU by process name or pattern. A cleanup
failure stops the workflow and leaves the exact private run root for explicit
recovery rather than broadening cleanup.

## Evidence and cleanup

Each stage emits a canonical receipt with input/command identity, return code,
bounded-output sizes and digests, truncation state, and a metadata-safe
classification. `Q1-EVIDENCE-MANIFEST.json` lists retained evidence in strict
UTF-8 lexical order. `SHA256SUMS` binds the exact retained membership, and
`Q1-EVIDENCE-SEAL.json` binds the manifest and sums, source commit/tree,
capsule plan, accepted Python artifacts, guest image/manifest, QEMU/helper
identity, and final posture. The seal is evidence only after independent root
replay; the capsule cannot promote itself to live-qualified.

Teardown first performs receipt-bound strict-CRI rollback inside the guest. It
then stops QEMU through the exact QMP/PID identity; process-name or pattern
signals are forbidden. Only exact owner-private `input`, `vm`, and `guest`
objects are removed. `evidence` and `cleanup` remain for independent review.
`verify-clean` accepts only those two directories and rejects symlinks,
special files, foreign ownership/devices, unexpected members, or excessive
evidence. Host KVM ACL rollback and trusted-host pre/post inventory equality
are separate final requirements.

No live-qualified claim is valid while a QEMU process, overlay, seed ISO,
private key, helper process, transaction, profile/workload, host ACL delta, or
unexplained host-runtime/network/service delta remains.
