INSERT INTO app_revisions(app_name, revision, spec_hash, spec_json, image, created_at, status)
VALUES(?,?,?,?,?,?,?)
ON CONFLICT(app_name, revision) DO NOTHING
