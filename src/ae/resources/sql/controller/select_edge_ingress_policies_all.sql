SELECT name, namespace, spec_json, status_json, created_at, updated_at
FROM edge_ingress_policies
ORDER BY namespace, name;
