SELECT replica_id, ready, live, status, readiness_message, liveness_message, exit_code, finished_at
FROM replica_status
WHERE app_name = ?
ORDER BY replica_id
