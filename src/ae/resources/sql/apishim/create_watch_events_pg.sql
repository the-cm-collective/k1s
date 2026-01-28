
                CREATE TABLE IF NOT EXISTS watch_events (
                  id BIGSERIAL PRIMARY KEY,
                  source TEXT NOT NULL,
                  grp TEXT NOT NULL,
                  ver TEXT NOT NULL,
                  res TEXT NOT NULL,
                  ns TEXT NOT NULL,
                  name TEXT NOT NULL,
                  ev_type TEXT NOT NULL,
                  rv BIGINT NOT NULL,
                  payload TEXT NOT NULL,
                  created_at DOUBLE PRECISION NOT NULL
                )
                