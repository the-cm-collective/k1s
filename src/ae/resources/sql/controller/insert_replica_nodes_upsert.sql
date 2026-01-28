INSERT INTO replica_nodes(app_name, replica_id, node_id, updated_at)
VALUES(?,?,?,?)
ON CONFLICT(app_name, replica_id) DO UPDATE SET
    node_id=excluded.node_id,
    updated_at=excluded.updated_at
