SELECT app_name, desired_replicas, ready_replicas, live_replicas, revision, revision_status, image, created, updated, removed, current_revision_ready_replicas, current_revision_live_replicas, old_revision_ready_replicas, old_revision_live_replicas, overlap_ready_replicas, overlap_live_replicas, ingress_host, ingress_path
FROM app_status WHERE app_name = ?
