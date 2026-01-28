INSERT INTO volume_attachments(app_name, volume_name, node_id, retention, created_at)
VALUES(?,?,?,?,?)
ON CONFLICT(app_name, volume_name) DO UPDATE SET
    node_id=excluded.node_id,
    retention=excluded.retention,
    created_at=excluded.created_at
