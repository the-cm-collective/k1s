INSERT INTO app_registry(app_name, spec_hash, spec_json, source, labels, updated_at, resource_version)
VALUES(?,?,?,?,?,?,?)
ON CONFLICT(app_name) DO UPDATE SET
    spec_hash=excluded.spec_hash,
    spec_json=excluded.spec_json,
    source=excluded.source,
    labels=excluded.labels,
    updated_at=excluded.updated_at,
    resource_version=excluded.resource_version
