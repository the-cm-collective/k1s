SELECT name, namespace, site_id, policy_name, policy_namespace, spec_json, status_json, created_at, updated_at
FROM edge_ingress_routes
ORDER BY namespace, name;
