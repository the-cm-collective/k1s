SELECT name, namespace, spec_json, created_at, updated_at
FROM edge_ingress_policies
WHERE name = ? AND namespace = ?
LIMIT 1;
