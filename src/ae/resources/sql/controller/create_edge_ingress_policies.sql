CREATE TABLE IF NOT EXISTS edge_ingress_policies (
  name TEXT NOT NULL,
  namespace TEXT NOT NULL,
  spec_json TEXT NOT NULL,
  status_json TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (name, namespace)
);
