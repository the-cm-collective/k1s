SELECT pod_name, ready, live, endpoint, status, readiness_message, liveness_message, exit_code, finished_at
FROM pod_status
WHERE app_name = ?
ORDER BY pod_name
