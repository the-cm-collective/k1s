
                CREATE TABLE IF NOT EXISTS app_status (
                    app_name TEXT PRIMARY KEY,
                    desired_replicas INTEGER NOT NULL,
                    ready_replicas INTEGER NOT NULL,
                    live_replicas INTEGER NOT NULL,
                    revision INTEGER NOT NULL,
                    revision_status TEXT NOT NULL,
                    image TEXT NOT NULL,
                    created INTEGER NOT NULL,
                    updated INTEGER NOT NULL,
                    removed INTEGER NOT NULL,
                    current_revision_ready_replicas INTEGER NOT NULL DEFAULT 0,
                    current_revision_live_replicas INTEGER NOT NULL DEFAULT 0,
                    old_revision_ready_replicas INTEGER NOT NULL DEFAULT 0,
                    old_revision_live_replicas INTEGER NOT NULL DEFAULT 0,
                    overlap_ready_replicas INTEGER NOT NULL DEFAULT 0,
                    overlap_live_replicas INTEGER NOT NULL DEFAULT 0,
                    ingress_host TEXT,
                    ingress_path TEXT
                )
                
