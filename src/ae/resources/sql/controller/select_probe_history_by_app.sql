SELECT replica_id, check_time, ready, live, readiness_message, liveness_message
FROM probe_history
WHERE app_name = ?
ORDER BY id DESC
LIMIT ?
