CREATE TABLE IF NOT EXISTS app_events (
    id __AUTO_INC__,
    app_name TEXT NOT NULL,
    revision INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    message TEXT NOT NULL,
    created_at TEXT NOT NULL
)
