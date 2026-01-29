INSERT INTO pod_nodes(app_name, pod_name, node_id, updated_at)
VALUES(?,?,?,?)
ON CONFLICT(app_name, pod_name) DO UPDATE SET
    node_id=excluded.node_id,
    updated_at=excluded.updated_at
