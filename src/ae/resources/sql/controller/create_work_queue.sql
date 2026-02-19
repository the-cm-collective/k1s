CREATE TABLE IF NOT EXISTS work_queue (
  work_id TEXT NOT NULL,
  attempt INTEGER NOT NULL,
  site_id TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  state TEXT NOT NULL,
  lease_id TEXT,
  leased_at TEXT,
  lease_expires_at TEXT,
  acked_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (work_id, attempt)
);

CREATE INDEX IF NOT EXISTS work_queue_state_idx ON work_queue(state);
CREATE INDEX IF NOT EXISTS work_queue_site_state_idx ON work_queue(site_id, state);
CREATE INDEX IF NOT EXISTS work_queue_lease_idx ON work_queue(lease_expires_at);
