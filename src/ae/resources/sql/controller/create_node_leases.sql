CREATE TABLE IF NOT EXISTS node_leases (
  node_id TEXT PRIMARY KEY,
  site_id TEXT NOT NULL,
  session_id TEXT NOT NULL,
  lease_id TEXT NOT NULL,
  controller_epoch INTEGER NOT NULL,
  lease_ttl_ms INTEGER NOT NULL,
  renew_after_ms INTEGER NOT NULL,
  last_renew_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS node_leases_site_idx ON node_leases(site_id);
CREATE INDEX IF NOT EXISTS node_leases_expires_idx ON node_leases(expires_at);
