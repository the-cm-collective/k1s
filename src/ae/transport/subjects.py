"""Subject helpers for Mode A transport."""

from __future__ import annotations


def local_work_subject(node_id: str) -> str:
    return f"k1s.v1.local.work.{node_id}"


def local_result_subject() -> str:
    return "k1s.v1.local.result"


def local_work_progress_subject() -> str:
    return "k1s.v1.local.work.progress"


def local_status_subject(node_id: str) -> str:
    return f"k1s.v1.local.status.{node_id}"


def local_logs_subject(node_id: str) -> str:
    return f"k1s.v1.local.logs.{node_id}"


def local_caps_subject(node_id: str) -> str:
    return f"k1s.v1.local.node.announce.{node_id}"


def hub_lease_acquire_subject(site_id: str) -> str:
    return f"k1s.v1.site.{site_id}.lease.acquire"


def hub_lease_renew_subject(site_id: str) -> str:
    return f"k1s.v1.site.{site_id}.lease.renew"


def hub_result_subject(site_id: str) -> str:
    return f"k1s.v1.site.{site_id}.result"


def hub_status_subject(site_id: str) -> str:
    return f"k1s.v1.site.{site_id}.status"


def hub_logs_subject(site_id: str) -> str:
    return f"k1s.v1.site.{site_id}.logs"


def hub_caps_subject(site_id: str) -> str:
    return f"k1s.v1.site.{site_id}.caps"


def hub_work_pull_subject(site_id: str) -> str:
    return f"k1s.v1.site.{site_id}.work.pull"


def hub_work_ack_subject(site_id: str) -> str:
    return f"k1s.v1.site.{site_id}.work.ack"


def work_stream_subject(site_id: str) -> str:
    return f"k1s.v1.work.site.{site_id}"
