CREATE TABLE IF NOT EXISTS service_endpoints (
    app_name TEXT NOT NULL,
    port INTEGER NOT NULL,
    ip TEXT NOT NULL,
    target_port INTEGER NOT NULL,
    ready INTEGER NOT NULL,
    PRIMARY KEY (app_name, port, ip)
)
