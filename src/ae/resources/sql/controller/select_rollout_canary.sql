SELECT weight, next_step_at, step, max, updated_at
FROM rollout_canary WHERE app_name = ?
