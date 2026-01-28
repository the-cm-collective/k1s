INSERT INTO objects (grp, ver, res, ns, name, metadata, spec, status, rv, created_at, updated_at)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT(grp, ver, res, ns, name)
DO UPDATE SET metadata=excluded.metadata, spec=excluded.spec, status=excluded.status,
              rv=excluded.rv, updated_at=excluded.updated_at
