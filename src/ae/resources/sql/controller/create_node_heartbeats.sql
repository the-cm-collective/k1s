CREATE TABLE IF NOT EXISTS node_heartbeats (
    node_id TEXT NOT NULL,
    status TEXT NOT NULL,
    seen_at TEXT NOT NULL,
    PRIMARY KEY (node_id)
)
