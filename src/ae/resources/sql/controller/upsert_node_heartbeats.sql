INSERT INTO node_heartbeats(node_id, status, seen_at)
VALUES(?,?,?)
ON CONFLICT(node_id) DO UPDATE SET status=excluded.status, seen_at=excluded.seen_at
