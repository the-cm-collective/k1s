
                CREATE TABLE IF NOT EXISTS nodes (
                    node_id TEXT PRIMARY KEY,
                    name TEXT,
                    labels TEXT DEFAULT '{}',
                    taints TEXT DEFAULT '[]',
                    backend TEXT,
                    endpoint TEXT,
                    pod_cidr TEXT,
                    wg_pubkey TEXT,
                    cordoned INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    rp_pubkey TEXT
                )
                
