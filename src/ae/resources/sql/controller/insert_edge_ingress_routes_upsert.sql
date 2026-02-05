INSERT INTO edge_ingress_routes
  (name, namespace, site_id, policy_name, policy_namespace, spec_json, created_at, updated_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(name, namespace) DO UPDATE SET
  site_id = excluded.site_id,
  policy_name = excluded.policy_name,
  policy_namespace = excluded.policy_namespace,
  spec_json = excluded.spec_json,
  updated_at = excluded.updated_at;
