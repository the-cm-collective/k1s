CREATE TABLE IF NOT EXISTS rollout_canary (
    app_name TEXT PRIMARY KEY,
    weight REAL NOT NULL,
    next_step_at TEXT NOT NULL,
    step REAL NOT NULL,
    max REAL NOT NULL,
    updated_at TEXT NOT NULL
)
