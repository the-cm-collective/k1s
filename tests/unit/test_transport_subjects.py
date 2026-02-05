from ae.transport import (
    hub_caps_subject,
    hub_lease_acquire_subject,
    hub_lease_renew_subject,
    hub_logs_subject,
    hub_result_subject,
    hub_status_subject,
    hub_work_ack_subject,
    hub_work_pull_subject,
    local_caps_subject,
    local_logs_subject,
    local_result_subject,
    local_status_subject,
    local_work_progress_subject,
    local_work_subject,
    work_stream_subject,
)


def test_subject_helpers() -> None:
    assert local_work_subject("node-1") == "k1s.v1.local.work.node-1"
    assert local_result_subject() == "k1s.v1.local.result"
    assert local_work_progress_subject() == "k1s.v1.local.work.progress"
    assert local_status_subject("node-1") == "k1s.v1.local.status.node-1"
    assert local_logs_subject("node-1") == "k1s.v1.local.logs.node-1"
    assert local_caps_subject("node-1") == "k1s.v1.local.node.announce.node-1"

    assert hub_lease_acquire_subject("site-1") == "k1s.v1.site.site-1.lease.acquire"
    assert hub_lease_renew_subject("site-1") == "k1s.v1.site.site-1.lease.renew"
    assert hub_result_subject("site-1") == "k1s.v1.site.site-1.result"
    assert hub_status_subject("site-1") == "k1s.v1.site.site-1.status"
    assert hub_logs_subject("site-1") == "k1s.v1.site.site-1.logs"
    assert hub_caps_subject("site-1") == "k1s.v1.site.site-1.caps"
    assert hub_work_pull_subject("site-1") == "k1s.v1.site.site-1.work.pull"
    assert hub_work_ack_subject("site-1") == "k1s.v1.site.site-1.work.ack"

    assert work_stream_subject("site-1") == "k1s.v1.work.site.site-1"
