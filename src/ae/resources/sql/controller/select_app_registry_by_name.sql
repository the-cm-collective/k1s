SELECT app_name, spec_hash, spec_json, source, labels, updated_at
FROM app_registry
WHERE app_name = ?
