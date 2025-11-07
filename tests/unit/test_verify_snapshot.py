import json
from pathlib import Path

from scripts.bench import verify_snapshot as VS


def test_verify_snapshot_summarize_matches_summary(tmp_path: Path) -> None:
    snap = tmp_path
    raw = snap / "raw"
    raw.mkdir(parents=True, exist_ok=True)

    # meta for nice headers
    (snap / "meta.json").write_text(
        json.dumps({"mode": "k1s", "label": "pods-1", "timestamp": "t"})
    )

    # two containers: one app (1 MiB) and one system (2 MiB)
    (raw / "containers_mem.csv").write_text(
        "container_id,name,pid,mem_current_bytes\n"
        "abc123def456,ae-echo,100,1048576\n"
        "fff111222333,caddy,200,2097152\n"
    )

    # podman inspect to label the app container
    (raw / "podman_inspect.json").write_text(
        json.dumps(
            [
                {
                    "Id": "abc123def4560000",
                    "Name": "ae-echo",
                    "Config": {"Labels": {"ae.app": "echo"}},
                },
                {"Id": "fff1112223339999", "Name": "caddy", "Config": {"Labels": {}}},
            ]
        )
    )

    # synthetic summary.json to compare with
    (snap / "summary.json").write_text(
        json.dumps(
            {
                "containers": {
                    "app_mem_bytes": 1048576,
                    "system_mem_bytes": 2097152,
                }
            }
        )
    )

    out = VS.summarize(snap)
    assert out["totals"]["app_bytes"] == 1048576
    assert out["totals"]["system_bytes"] == 2097152
    assert out["summary_json_match"]["app_match"] is True
    assert out["summary_json_match"]["system_match"] is True
