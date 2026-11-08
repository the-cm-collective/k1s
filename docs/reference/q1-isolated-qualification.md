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
- at least 64 GiB of free space on the filesystem containing `/tmp`.

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
posture, acceptance order, and exact `/tmp/k1s-q1-<32-hex>` layout.

Example shape:

```text
python3 scripts/qualification/kp1_q1_capsule.py plan \
  --run-id q1-0123456789abcdef0123456789abcdef \
  --source-commit COMMIT --source-tree TREE \
  --base-image /absolute/base.qcow2 \
  --expect-base-image-sha256 sha256:... \
  --guest-manifest /absolute/q1-guest-manifest-v1.json \
  --expect-guest-manifest-sha256 sha256:... \
  --wheel /absolute/k1s_core-0.1.8a1-py3-none-any.whl \
  --expect-wheel-sha256 sha256:... \
  --sdist /absolute/k1s_core-0.1.8a1.tar.gz \
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
  --output-root /tmp/k1s-q1-0123456789abcdef0123456789abcdef \
  --output /absolute/private/plan.json
```

The output file's parent must already be an owner-only mode-`0700`
directory. The plan remains private because it contains exact host input
paths. Public evidence contains identities and outcomes, not those paths.

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
identity, and access for the exact user. If the exact user lacks access, the
only separately approved host mutation is one temporary `rw` ACL entry on that
already-attested device. Rollback removes only the newly added ACL entry after
the same identity is revalidated. The capsule never creates or removes the
device and never changes its owner or mode.

`run` requires the canonical Gate A attestation, its file digest, the exact
plan digest, and the literal confirmation `RUN_ID:PLAN_DIGEST`.

Teardown is also digest- and confirmation-bound. It accepts only the exact
canonical `guest/run.json`, its file digest, and
`RUN_ID:RUN_RECEIPT_DIGEST`, after the recorded QEMU identity is absent.
`verify-clean` accepts the exact canonical cleanup receipt and its file digest;
neither command performs ambient process or filesystem discovery.

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
