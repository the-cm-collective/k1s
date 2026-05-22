# ae.k8s

- Source folder: `src/ae/k8s`
- Last reviewed: 2026-05-13

## System Summary
Kubernetes import/export/check layer that translates between k1s manifests and Kubernetes-style resources.

## Package Initializer
Kubernetes-related helpers (export, checks).

## Module And Script Map
| File | Detailed doc | Functionality | Important entry points |
| --- | --- | --- | --- |
| check.py | [docs/check.md](docs/check.md) | K8s portability checker for AppManifest. | Issue |
| convert.py | [docs/convert.md](docs/convert.md) | Kubernetes manifest conversion helpers for k1s. | app_name_for_k8s, _get, _metadata, _spec, _name |
| exporter.py | [docs/exporter.md](docs/exporter.md) | Kubernetes exporter: convert AppManifest to upstream K8s YAML. | ExportOptions |
| presets.py | [docs/presets.md](docs/presets.md) | Presets for export-k8s to speed up common profiles. | apply_preset, apply_ingress_preset |
| validate.py | [docs/validate.md](docs/validate.md) | Lightweight structural validator for exported K8s YAML. | validate_documents |

## Cross-Package Dependencies
`ae.controller.spec`, `ae.k8s.exporter`

## Maintenance Notes
Detailed markers live in the per-module docs; direct module counts:
- `exporter.py`: 1 marker(s)

## Related Tests
- `tests/unit/test_cri_runtime_apishim_reads.py`
- `tests/unit/test_ingress_preset.py`
- `tests/unit/test_k8s_check_policy.py`
- `tests/unit/test_k8s_convert_ingress.py`
- `tests/unit/test_k8s_convert_pvc.py`
- `tests/unit/test_k8s_convert_service.py`
- `tests/unit/test_k8s_convert_statefulset.py`
- `tests/unit/test_k8s_explicit_volumes.py`
- `tests/unit/test_k8s_exporter.py`
- `tests/unit/test_k8s_ingress.py`
- `tests/unit/test_k8s_multi_container.py`
- `tests/unit/test_k8s_statefulset_export.py`
- `tests/unit/test_k8s_validate_and_preset.py`
- `tests/unit/test_topology_spread.py`
