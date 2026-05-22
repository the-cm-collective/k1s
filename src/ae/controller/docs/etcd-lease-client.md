# Etcd Lease Client

- Source: `controller/etcd_lease_client.py`
- Last reviewed: 2026-05-13
- Size: 320 lines

## Purpose
Provides classes LeaseKeepAliveResult, GrpcEtcdLeaseClient within Core control plane: manifest loading, reconcile loop, state stores, scheduling, HA authority, and workload controllers.

## Public Surface And Internal Entry Points
### Classes
| Class | Line | Summary | Notes |
| --- | --- | --- | --- |
| LeaseKeepAliveResult | 154 | No class docstring. |  |
| GrpcEtcdLeaseClient | 159 | Minimal etcd Lease gRPC client. | public methods: from_env, grant_lease, keepalive, revoke_lease, close |

### Top-Level Functions
| Function | Line | Kind | Summary |
| --- | --- | --- | --- |
| _grpc_required_error | 20 | function | Internal helper. |
| _encode_varint | 30 | function | Internal helper. |
| _decode_varint | 42 | function | Internal helper. |
| _skip_wire_value | 58 | function | Internal helper. |
| _encode_lease_grant_request | 72 | function | Internal helper. |
| _decode_lease_grant_response | 82 | function | Internal helper. |
| _encode_lease_keepalive_request | 102 | function | Internal helper. |
| _decode_lease_keepalive_response | 109 | function | Internal helper. |
| _encode_lease_revoke_request | 129 | function | Internal helper. |
| _decode_lease_revoke_response | 136 | function | Internal helper. |
| _normalize_grpc_target | 144 | function | Internal helper. |

## Runtime And Data Flow
- External libraries: `grpc`
- Side-effect surfaces: filesystem/state, network/API.

## Maintenance Notes
No explicit deprecated/TODO/legacy/fallback/workaround markers were found in this module during static review.

## Related Tests And Docs
- `tests/unit/test_etcd_lease_client.py`
