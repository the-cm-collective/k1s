import json
from pathlib import Path

from scripts.bench import mem_aggregate as MA


def test_podman_inspect_labels_classification(tmp_path: Path) -> None:
    snap = tmp_path
    raw = snap / "raw"
    raw.mkdir(parents=True, exist_ok=True)

    # minimal meta
    (snap / "meta.json").write_text(json.dumps({"mode": "k1s", "label": "t"}))

    # containers csv with two containers (one app, one system)
    (raw / "containers_mem.csv").write_text(
        "container_id,name,pid,mem_current_bytes\n"
        "abc123def456,ae-echo,100,1048576\n"  # 1 MiB app
        "fff111222333,caddy,200,2097152\n"  # 2 MiB system
    )

    # podman inspect providing labels for app classification
    podman_inspect = [
        {
            "Id": "abc123def4560000",  # longer id; we slice to 12
            "Name": "ae-echo",
            "Config": {"Labels": {"ae.app": "echo"}},
        },
        {
            "Id": "fff1112223339999",
            "Name": "caddy",
            "Config": {"Labels": {}},
        },
    ]
    (raw / "podman_inspect.json").write_text(json.dumps(podman_inspect))

    summary = MA.aggregate(snap)
    assert summary["containers"]["app_mem_bytes"] == 1048576
    assert summary["containers"]["system_mem_bytes"] == 2097152
