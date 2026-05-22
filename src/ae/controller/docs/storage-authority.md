# Storage Authority

- Source: `controller/storage_authority.py`
- Last reviewed: 2026-05-13
- Size: 134 lines

## Purpose
Leader-owned HA storage controller hosting for shared-authority storage resources.

## Public Surface And Internal Entry Points
### Classes
| Class | Line | Summary | Notes |
| --- | --- | --- | --- |
| StorageAuthorityRunner | 28 | Run the storage reconciliation engine only while this controller is leader. | public methods: start, stop |

### Top-Level Functions
| Function | Line | Kind | Summary |
| --- | --- | --- | --- |
| build_storage_authority_store | 20 | function | Build the HA apishim-store multiplexer used by the controller storage runner. |

## Runtime And Data Flow
- Internal dependencies: `ae.apishim.ha_store`, `ae.apishim.store`, `ae.controller.state`, `ae.storage.controller`
- Environment inputs: `AE_APISHIM_DB`, `AE_APISHIM_DSN`
- Side-effect surfaces: filesystem/state.

## Maintenance Notes
- Line 24: `legacy = ObjectStore(db_path=db_path, dsn=dsn)`
- Line 25: `return MultiplexApishimStore.from_state_and_legacy(state, legacy)`

## Related Tests And Docs
- `tests/unit/test_storage_authority.py`
