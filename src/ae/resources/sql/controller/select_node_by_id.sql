SELECT n.node_id, n.name, n.labels, n.capabilities_json, n.taints, n.backend, n.endpoint, n.pod_cidr, n.wg_pubkey, n.rp_pubkey, n.created_at, n.updated_at,
       n.cordoned, hb.status, hb.seen_at,
       COALESCE(nl.generation, 0), nl.site_id, nl.expires_at
FROM nodes n
LEFT JOIN node_heartbeats hb ON hb.node_id = n.node_id
LEFT JOIN node_leases nl ON nl.node_id = n.node_id
WHERE n.node_id = ?
