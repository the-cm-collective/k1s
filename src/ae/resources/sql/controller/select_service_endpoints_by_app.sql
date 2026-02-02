SELECT app_name, port, ip, target_port, ready
FROM service_endpoints
WHERE app_name = ?
ORDER BY port, ip
