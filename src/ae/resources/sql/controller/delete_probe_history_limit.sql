DELETE FROM probe_history
WHERE id IN (
    SELECT id FROM probe_history
    WHERE app_name = ? AND replica_id = ?
    ORDER BY id DESC
    LIMIT -1 OFFSET 50
)
