CREATE TABLE IF NOT EXISTS app_revisions (
    app_name TEXT NOT NULL,
    revision INTEGER NOT NULL,
    spec_hash TEXT NOT NULL,
    spec_json TEXT NOT NULL,
    image TEXT NOT NULL,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL,
    PRIMARY KEY (app_name, revision)
)
