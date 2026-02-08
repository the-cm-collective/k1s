SELECT rs.pod_name, rn.node_id, rs.ready, rs.live, rs.status, rs.readiness_message, rs.liveness_message
FROM pod_status rs
LEFT JOIN pod_nodes rn ON rs.app_name = rn.app_name AND rs.pod_name = rn.pod_name
WHERE rs.app_name = ?
UNION ALL
SELECT rn.pod_name, rn.node_id, rs.ready, rs.live, rs.status, rs.readiness_message, rs.liveness_message
FROM pod_nodes rn
LEFT JOIN pod_status rs ON rs.app_name = rn.app_name AND rs.pod_name = rn.pod_name
WHERE rn.app_name = ? AND rs.pod_name IS NULL
ORDER BY pod_name
