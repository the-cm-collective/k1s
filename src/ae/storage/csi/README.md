# ae.storage.csi

- Source folder: `src/ae/storage/csi`
- Last reviewed: 2026-05-13

## System Summary
CSI support package with a handwritten client and generated CSI protobuf bindings.

## Subsystems
- StorageClass/PVC/PV authority and reconcile behavior.
- NetFS provisioning/mount lifecycle and node-side volume management.
- CSI client integration and storage state adapters for shim-backed reads.

## Package Initializer
CSI gRPC client utilities. Defines explicit exports.

## Module And Script Map
| File | Detailed doc | Functionality | Important entry points |
| --- | --- | --- | --- |
| client.py | [docs/client.md](docs/client.md) | CSI gRPC client helpers for controller and node operations. | CsiVolume, CsiControllerClient, CsiNodeClient |

## Resource And Generated Subtrees
| Folder | Files | Types | Review policy |
| --- | --- | --- | --- |
| api | 4 | .proto:1, .py:3 | Generated/vendor/static/resource subtree; summarized at folder level. |

## Cross-Package Dependencies
`.api`

## Maintenance Notes
- No explicit deprecated/TODO/legacy/fallback markers were found in direct modules during static review.

## Related Tests
- No direct package-level test reference found by static search.
