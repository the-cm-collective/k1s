CREATE TABLE IF NOT EXISTS probe_history (
    id __AUTO_INC__,
    app_name TEXT NOT NULL,
    pod_name TEXT NOT NULL,
    check_time TEXT NOT NULL,
    ready INTEGER NOT NULL,
    live INTEGER NOT NULL,
    readiness_message TEXT NOT NULL,
    liveness_message TEXT NOT NULL
)
