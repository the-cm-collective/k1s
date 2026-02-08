INSERT INTO pod_status(
    app_name,
    pod_name,
    ready,
    live,
    endpoint,
    status,
    readiness_message,
    liveness_message,
    exit_code,
    finished_at,
    updated_at
)
VALUES(?,?,?,?,?,?,?,?,?,?,?)
ON CONFLICT(app_name, pod_name) DO UPDATE SET
    ready=excluded.ready,
    live=excluded.live,
    endpoint=excluded.endpoint,
    status=excluded.status,
    readiness_message=excluded.readiness_message,
    liveness_message=excluded.liveness_message,
    exit_code=excluded.exit_code,
    finished_at=excluded.finished_at,
    updated_at=excluded.updated_at
