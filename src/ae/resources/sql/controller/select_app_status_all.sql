SELECT app_name, desired_replicas, ready_replicas, live_replicas, revision, revision_status, image, created, updated, removed, ingress_host, ingress_path
FROM app_status ORDER BY app_name
