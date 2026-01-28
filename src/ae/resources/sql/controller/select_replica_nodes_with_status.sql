SELECT rs.replica_id, rn.node_id, rs.ready, rs.live, rs.status, rs.readiness_message, rs.liveness_message
FROM replica_status rs
LEFT JOIN replica_nodes rn ON rs.app_name = rn.app_name AND rs.replica_id = rn.replica_id
WHERE rs.app_name = ?
ORDER BY rs.replica_id
