
                INSERT INTO app_status(app_name, desired_replicas, ready_replicas, live_replicas, revision, revision_status, image, created, updated, removed, current_revision_ready_replicas, current_revision_live_replicas, old_revision_ready_replicas, old_revision_live_replicas, overlap_ready_replicas, overlap_live_replicas, ingress_host, ingress_path)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(app_name) DO UPDATE SET
                    desired_replicas=excluded.desired_replicas,
                    ready_replicas=excluded.ready_replicas,
                    live_replicas=excluded.live_replicas,
                    revision=excluded.revision,
                    revision_status=excluded.revision_status,
                    image=excluded.image,
                    created=excluded.created,
                    updated=excluded.updated,
                    removed=excluded.removed,
                    current_revision_ready_replicas=excluded.current_revision_ready_replicas,
                    current_revision_live_replicas=excluded.current_revision_live_replicas,
                    old_revision_ready_replicas=excluded.old_revision_ready_replicas,
                    old_revision_live_replicas=excluded.old_revision_live_replicas,
                    overlap_ready_replicas=excluded.overlap_ready_replicas,
                    overlap_live_replicas=excluded.overlap_live_replicas,
                    ingress_host=excluded.ingress_host,
                    ingress_path=excluded.ingress_path
                
