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

### NetFS StorageClass config

Provide StorageClass definitions via `AE_STORAGE_PROVISIONERS` (YAML file). The
shim will seed these into its object store on startup.

Example (see `docs/reference/storage-classes.yaml`):

```
export AE_STORAGE_PROVISIONERS=docs/reference/storage-classes.yaml
```

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
