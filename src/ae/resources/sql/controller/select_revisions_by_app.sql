SELECT revision, spec_hash, status, image, created_at
FROM app_revisions
WHERE app_name = ?
ORDER BY revision DESC
LIMIT ?
