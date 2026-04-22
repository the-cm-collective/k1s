# API Shim

The API shim exposes a Kubernetes-compatible API surface for `kubectl`, `helm`, ingress controllers, and other tooling that expects Kubernetes discovery and object shapes. Use this page as the landing page for the shim. The raw `/openapi/v3` endpoint intentionally returns JSON; use Swagger or ReDoc when browsing in a browser.

## Primary surfaces
- [Swagger UI](/swagger/apishim)
- [ReDoc](/redoc/apishim)
- [OpenAPI v3](/openapi/v3)
- [OpenAPI v2 compatibility mirror](/openapi/v2)

## What it covers
- Discovery endpoints such as `/api`, `/apis`, and preferred versions
- Core workload, service, ingress, RBAC, and patch/apply flows for the supported k1s compatibility subset
- Kubernetes-facing clients such as `kubectl`, Helm dry-run/apply flows, and compatibility-oriented controllers or dashboards

## Current reference pages
- [API Shim Compatibility Matrix](apishim-compatibility-matrix.html)
- [API Shim Roadmap](apishim-roadmap.html)
- [K8s Portability / Compliance Status](k8s-compliance.html)
- [HTTP API](http-api.html) for controller-native operational endpoints

## Notes
- `/openapi/v3` is the primary published shim schema; `/openapi/v2` remains the compatibility mirror.
- Browser readers should prefer Swagger or ReDoc. Automation and generated clients should use the raw OpenAPI documents.
