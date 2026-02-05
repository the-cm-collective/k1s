CREATE TABLE IF NOT EXISTS work_ledger (
  work_id TEXT PRIMARY KEY,
  attempt INTEGER NOT NULL,
  site_id TEXT NOT NULL,
  state TEXT NOT NULL,
  desired_generation INTEGER,
  assigned_node_id TEXT,
  observed_generation INTEGER,
  result_json TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  state_updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS work_ledger_state_idx ON work_ledger(state);
CREATE INDEX IF NOT EXISTS work_ledger_site_idx ON work_ledger(site_id);
