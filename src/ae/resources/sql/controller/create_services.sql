CREATE TABLE IF NOT EXISTS services (
    app_name TEXT PRIMARY KEY,
    cluster_ip TEXT NOT NULL,
    ports TEXT NOT NULL,
    created_at TEXT NOT NULL
)
