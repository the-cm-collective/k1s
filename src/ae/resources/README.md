# ae.resources

- Source folder: `src/ae/resources`
- Last reviewed: 2026-05-13

## System Summary
Package data loader plus bundled SQL, dashboard/docs HTML, and ingress templates.

## Package Initializer
Packaged text resources for the ae codebase.

## Module And Script Map
| File | Detailed doc | Functionality | Important entry points |
| --- | --- | --- | --- |
| loader.py | [docs/loader.md](docs/loader.md) | Resource loaders for packaged text assets. | load_text, render_text |

## Resource And Generated Subtrees
| Folder | Files | Types | Review policy |
| --- | --- | --- | --- |
| ingress | 1 | .txt:1 | Generated/vendor/static/resource subtree; summarized at folder level. |
| observability | 4 | .html:4 | Generated/vendor/static/resource subtree; summarized at folder level. |
| sql | 77 | .sql:77 | Generated/vendor/static/resource subtree; summarized at folder level. |

## Maintenance Notes
- No explicit deprecated/TODO/legacy/fallback markers were found in direct modules during static review.

## Related Tests
- `tests/unit/test_dashboard_template_ha.py`
- `tests/unit/test_http_api_status_detail.py`
- `tests/unit/test_resources_loader.py`
