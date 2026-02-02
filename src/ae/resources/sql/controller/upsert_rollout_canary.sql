INSERT INTO rollout_canary(app_name, weight, next_step_at, step, max, updated_at)
VALUES(?,?,?,?,?,?)
ON CONFLICT(app_name) DO UPDATE SET weight=excluded.weight, next_step_at=excluded.next_step_at, step=excluded.step, max=excluded.max, updated_at=excluded.updated_at
