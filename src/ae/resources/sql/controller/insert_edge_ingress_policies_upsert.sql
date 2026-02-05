INSERT INTO edge_ingress_policies
  (name, namespace, spec_json, status_json, created_at, updated_at)
VALUES (?, ?, ?, ?, ?, ?)
ON CONFLICT(name, namespace) DO UPDATE SET
  spec_json = excluded.spec_json,
  status_json = excluded.status_json,
  updated_at = excluded.updated_at;
