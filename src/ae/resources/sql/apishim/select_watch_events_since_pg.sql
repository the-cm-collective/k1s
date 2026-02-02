SELECT id, source, grp, ver, res, ns, name, ev_type, rv, payload
FROM watch_events
WHERE id > %s
ORDER BY id
LIMIT %s
