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
