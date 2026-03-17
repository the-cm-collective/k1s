CREATE TABLE IF NOT EXISTS work_outbox (
  work_id TEXT NOT NULL,
  attempt INTEGER NOT NULL,
  site_id TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  publish_subject TEXT,
  publish_msg_id TEXT,
  state TEXT NOT NULL,
  publish_attempts INTEGER NOT NULL DEFAULT 0,
  last_publish_at TEXT,
  last_publish_error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (work_id, attempt)
);

CREATE INDEX IF NOT EXISTS work_outbox_state_idx ON work_outbox(state);
CREATE INDEX IF NOT EXISTS work_outbox_site_state_idx ON work_outbox(site_id, state);
