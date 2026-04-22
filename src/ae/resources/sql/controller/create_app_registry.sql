CREATE TABLE IF NOT EXISTS app_registry (
    app_name TEXT PRIMARY KEY,
    spec_hash TEXT NOT NULL,
    spec_json TEXT NOT NULL,
    source TEXT NOT NULL,
    labels TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    resource_version INTEGER NOT NULL DEFAULT 0
)
