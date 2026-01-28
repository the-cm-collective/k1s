
                INSERT INTO nodes(node_id, name, labels, taints, backend, endpoint, pod_cidr, wg_pubkey, cordoned, created_at, updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(node_id) DO UPDATE SET
                    name=excluded.name,
                    labels=excluded.labels,
                    taints=excluded.taints,
                    backend=excluded.backend,
                    endpoint=excluded.endpoint,
                    pod_cidr=excluded.pod_cidr,
                    wg_pubkey=excluded.wg_pubkey,
                    cordoned=excluded.cordoned,
                    updated_at=excluded.updated_at
                