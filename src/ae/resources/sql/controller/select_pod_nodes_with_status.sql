SELECT rs.pod_name, rn.node_id, rs.ready, rs.live, rs.status, rs.readiness_message, rs.liveness_message
FROM pod_status rs
LEFT JOIN pod_nodes rn ON rs.app_name = rn.app_name AND rs.pod_name = rn.pod_name
WHERE rs.app_name = ?
ORDER BY rs.pod_name
