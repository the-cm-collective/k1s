INSERT INTO services(app_name, cluster_ip, ports, created_at)
VALUES(?,?,?,?)
ON CONFLICT(app_name) DO UPDATE SET cluster_ip=excluded.cluster_ip, ports=excluded.ports
