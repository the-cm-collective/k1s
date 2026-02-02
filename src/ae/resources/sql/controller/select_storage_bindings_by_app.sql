SELECT app_name, volume_name, node_id, retention, created_at
FROM storage_bindings
WHERE app_name = ?
ORDER BY volume_name
