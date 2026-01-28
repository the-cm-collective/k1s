CREATE TABLE IF NOT EXISTS replica_nodes (
    app_name TEXT NOT NULL,
    replica_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (app_name, replica_id)
)
