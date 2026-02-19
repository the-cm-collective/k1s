CREATE TABLE IF NOT EXISTS edge_ingress_routes (
  name TEXT NOT NULL,
  namespace TEXT NOT NULL,
  site_id TEXT NOT NULL,
  policy_name TEXT,
  policy_namespace TEXT,
  spec_json TEXT NOT NULL,
  status_json TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (name, namespace)
);

CREATE INDEX IF NOT EXISTS edge_ingress_routes_site_idx ON edge_ingress_routes(site_id);
