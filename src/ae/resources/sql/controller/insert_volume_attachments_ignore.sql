INSERT INTO volume_attachments(app_name, volume_name, node_id, retention, created_at)
VALUES(?,?,?,?,?)
ON CONFLICT(app_name, volume_name) DO NOTHING
