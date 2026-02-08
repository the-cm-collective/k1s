
                CREATE TABLE IF NOT EXISTS pod_status (
                    app_name TEXT NOT NULL,
                    pod_name TEXT NOT NULL,
                    ready INTEGER NOT NULL,
                    live INTEGER NOT NULL,
                    endpoint TEXT,
                    status TEXT NOT NULL,
                    readiness_message TEXT NOT NULL,
                    liveness_message TEXT NOT NULL,
                    exit_code INTEGER,
                    finished_at TEXT,
                    updated_at TEXT,
                    PRIMARY KEY (app_name, pod_name)
                )
                
