UPDATE app_revisions
SET status = ?, image = ?, spec_hash = spec_hash
WHERE app_name = ? AND revision = ?
