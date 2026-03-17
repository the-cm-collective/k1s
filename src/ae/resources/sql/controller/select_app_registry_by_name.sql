SELECT app_name, spec_hash, spec_json, source, labels, updated_at, resource_version
FROM app_registry
WHERE app_name = ?
