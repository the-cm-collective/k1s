SELECT n.node_id, n.name, n.labels, n.capabilities_json, n.taints, n.backend, n.endpoint, n.pod_cidr, n.wg_pubkey, n.rp_pubkey, n.created_at, n.updated_at,
       n.cordoned, hb.status, hb.seen_at
FROM nodes n
LEFT JOIN node_heartbeats hb ON hb.node_id = n.node_id
ORDER BY n.node_id
