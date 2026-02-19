CREATE TABLE IF NOT EXISTS site_ingress_endpoints (
  site_id TEXT PRIMARY KEY,
  mode TEXT NOT NULL,
  core_proxy_port INTEGER,
  public_urls_json TEXT,
  quarantine_until TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS site_ingress_core_port_idx
  ON site_ingress_endpoints(core_proxy_port);
