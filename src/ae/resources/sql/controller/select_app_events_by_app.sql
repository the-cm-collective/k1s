SELECT revision, event_type, message, created_at
FROM app_events
WHERE app_name = ?
ORDER BY id DESC
LIMIT ?
