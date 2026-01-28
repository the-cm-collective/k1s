CREATE TABLE IF NOT EXISTS volume_attachments (
    app_name TEXT NOT NULL,
    volume_name TEXT NOT NULL,
    node_id TEXT NOT NULL,
    retention TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (app_name, volume_name)
)
