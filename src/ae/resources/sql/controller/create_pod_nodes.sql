CREATE TABLE IF NOT EXISTS pod_nodes (
    app_name TEXT NOT NULL,
    pod_name TEXT NOT NULL,
    node_id TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (app_name, pod_name)
)
