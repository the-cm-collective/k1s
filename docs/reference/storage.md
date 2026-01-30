## Storage (PV-lite)

Use `spec.storage` to declare named persistent volumes for an app. The controller
creates a container‑engine named volume per entry and mounts it into the container.

### Spec

```
spec:
  storage:
    - name: data
      mountPath: /var/lib/app
      retention: Retain   # or Delete
```

- name: logical name for the volume; the engine volume will be named `ae-<app>-<name>`
- mountPath: container path where the volume is mounted
- retention: `Retain` (default) keeps the volume on `ae delete`; `Delete` removes it on `ae delete --purge`

### CLI

List volumes:

```
ae volumes list          # all app volumes
#ae volumes list --app echo
```

Delete with purge (removes storage volumes with retention=Delete):

```
ae delete echo --purge
```

### Notes

- HostPath binds under `spec.volumes` still work for simple dev paths.
- Future: `size` is reserved for advisory/validation.
- K8s-style hostPath objects (`{ path, type }`) are accepted; `type` is ignored.
- apishim converts K8s hostPath and PVC volumes into `spec.volumes` and `spec.pvcMounts`.

### NetFS StorageClass config

Provide StorageClass definitions via `AE_STORAGE_PROVISIONERS` (YAML file). The
shim will seed these into its object store on startup.

Example (see `docs/reference/storage-classes.yaml`):

```
export AE_STORAGE_PROVISIONERS=docs/reference/storage-classes.yaml
```

For dynamic NFS provisioning, include at least:
- `parameters.server`: NFS server host/IP
- `parameters.path`: exported base path (the controller will use a per-PVC subdir when possible)
- `parameters.hostPath` (optional): local directory for the exported path; when set, the controller
  will create and clean up per-PVC directories under this root.

### NetFS PVC mounts (apishim)

When running the node agent with `AE_ENABLE_NETFS=1`, PVC mounts from apishim
workloads are resolved into hostPath mounts under `AE_NETFS_ROOT`. The agent
currently reads PVC/PV bindings from the apishim store (sqlite/postgres) to
locate the bound PV.

```
export AE_ENABLE_NETFS=1
export AE_APISHIM_DB=state/apishim.db
```

NetFS currently supports NFS PVs. The node must have `mount`/`umount` plus an
NFS helper (`mount.nfs` or `mount.nfs4`). If these tools or the NFS mount fail,
the agent records a PVC warning event; use `kubectl get events -n <ns>` to inspect
`NfsPrereqFailed`, `MountFailed`, or `MountConflict` reasons.

### NetFS capability matrix (current)

| Backend | Dynamic provisioning | Attach/Detach | Mounts | Notes |
| --- | --- | --- | --- | --- |
| NFS (`k1s.io/nfs`) | Yes | No | Yes | RWX supported, mountOptions honored. |
| CSI (static PVs) | No | Controller creates VolumeAttachment | Marker only | Requires PV.spec.csi.driver + volumeHandle; no CSI gRPC. |
| local-path (`k1s.io/local-path`) | Yes | No | Yes | HostPath-backed (not network). |

### NetFS cloning (PVC dataSourceRef)

For hostPath-backed provisioners (NFS/local-path), the controller can restore a
new PVC from a source PVC:

- Set `spec.dataSourceRef` with kind `PersistentVolumeClaim`.
- The source PVC must be `Bound`.
- Source and target must use the same StorageClass.
- Filesystem volumes only (`volumeMode: Filesystem`).

Example: `specs/examples/netfs-clone.yaml`.

### NetFS block devices

Block volumes are supported for hostPath-backed provisioners:

- PVC must set `volumeMode: Block`.
- k1s manifests should use `spec.pvcMounts[].devicePath` to map the device
  into the container (see `specs/examples/netfs-block-device.yaml`).

### CSIStorageCapacity overrides

StorageClasses can publish static capacity (useful for external CSI drivers):

- `parameters.capacity`: Kubernetes size string (e.g., `10Gi`).
- `parameters.capacityBytes`: integer byte count.

Example: `specs/examples/netfs-capacity-override.yaml`.

### SELinux relabeling

When `seLinuxOptions` are provided via `podSecurity`, k1s attempts a best‑effort
`chcon` on the mount path. For RWX/ROX volumes, set `AE_NETFS_SELINUX_RECURSIVE=1`
to allow recursive relabeling.


### Retention & Purge

- `retention: Retain` (default) keeps data even if you delete the app:
  - `ae delete <app>` will stop and remove containers but keep the engine volume(s).
- `retention: Delete` removes data only when you purge:
  - `ae delete <app> --purge`

Example with `retention: Delete` (see `specs/examples/echo-storage-delete.yaml`):

```
spec:
  storage:
    - name: data
      mountPath: /var/lib/echo
      retention: Delete
```

### NetFS smoke test

If apishim + controller + node agent are running, you can exercise static PV/PVC
binding and a PVC-backed Deployment:

```
scripts/netfs_smoke.sh
```

To exercise dynamic provisioning, run:

```
NETFS_DYNAMIC=1 scripts/netfs_smoke.sh
```

### NetFS NFS harness

For an end-to-end NFS mount harness (NFS server container, apishim, node agent,
and smoke test), run:

```
scripts/netfs_nfs_harness.sh
```

NFS mounts typically require root. Run the harness with `sudo` to exercise the
mount path, or set `SKIP_MOUNT_PREFLIGHT=1` to continue without the preflight
check. Use `KEEP_STATE=1` to preserve logs/state for debugging.
