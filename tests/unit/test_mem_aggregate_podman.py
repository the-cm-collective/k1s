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
    assert summary["containers"]["app_container_count"] == 1
    assert summary["containers"]["system_container_count"] == 1
    assert summary["containers"]["total_container_count"] == 2


def test_host_system_cgroups_csv_drives_summary_top_breakdown(tmp_path: Path) -> None:
    snap = tmp_path
    raw = snap / "raw"
    raw.mkdir(parents=True, exist_ok=True)

    (snap / "meta.json").write_text(json.dumps({"mode": "k1s", "label": "t"}))
    (raw / "host_system_cgroups.csv").write_text(
        "path,bytes,slice_kind\n"
        "/system.slice/low.service,1048576,system.slice\n"
        "/init.scope,524288,init.scope\n"
        "/system.slice/high.service,2097152,system.slice\n"
    )

    summary = MA.aggregate(snap)

    assert summary["overhead"]["host_system_cgroups_bytes"] == 3670016
    assert summary["overhead"]["host_system_cgroups_top"] == [
        {
            "path": "/system.slice/high.service",
            "slice_kind": "system.slice",
            "bytes": 2097152,
            "mib": 2.0,
        },
        {
            "path": "/system.slice/low.service",
            "slice_kind": "system.slice",
            "bytes": 1048576,
            "mib": 1.0,
        },
        {
            "path": "/init.scope",
            "slice_kind": "init.scope",
            "bytes": 524288,
            "mib": 0.5,
        },
    ]


def test_runtime_attribution_for_cri_lane_filters_foreign_runtime_and_counts_shims(
    tmp_path: Path,
) -> None:
    snap = tmp_path
    raw = snap / "raw"
    raw.mkdir(parents=True, exist_ok=True)

    (snap / "meta.json").write_text(
        json.dumps({"mode": "k1s", "label": "t", "backend": "cri", "engine_filter": "cri"})
    )
    (raw / "ps_before.txt").write_text(
        "PID PPID COMMAND RSS\n"
        "101 1 containerd 0\n"
        "102 1 containerd-shim-runc-v2 0\n"
        "103 1 conmon 0\n"
        "104 1 dockerd 0\n"
        "105 1 passt.avx2 0\n"
    )
    (raw / "ps_scan_before.txt").write_text(
        "PID PPID COMMAND COMMAND\n"
        "101 1 containerd /usr/bin/containerd\n"
        "102 1 containerd-shim-runc-v2 /usr/bin/containerd-shim-runc-v2 -namespace k8s.io\n"
        "103 1 conmon /usr/bin/conmon --api-version 1\n"
        "104 1 dockerd /usr/bin/dockerd\n"
        "105 1 passt.avx2 /usr/bin/passt.avx2 --some-podman-helper\n"
    )
    (raw / "smaps_101_containerd.txt").write_text("Pss:               1024 kB\n")
    (raw / "smaps_102_containerd-shim-runc-v2.txt").write_text("Pss:               2048 kB\n")
    (raw / "smaps_103_conmon.txt").write_text("Pss:                512 kB\n")
    (raw / "smaps_104_dockerd.txt").write_text("Pss:                256 kB\n")
    (raw / "smaps_105_passt.avx2.txt").write_text("Pss:               4096 kB\n")

    summary = MA.aggregate(snap)

    assert summary["overhead"]["pss_kb_runtime"] == 3072
    assert summary["overhead"]["runtime_process_groups"] == {
        "containerd": 1024,
        "containerd_shim": 2048,
        "conmon": 0,
        "podman": 0,
        "passt": 0,
        "slirp4netns": 0,
        "dockerd": 0,
        "other_runtime": 0,
    }
    assert summary["overhead"]["runtime_process_group_stats"] == {
        "containerd": {
            "count": 1,
            "pss_kb": 1024,
            "pss_mib": 1.0,
            "mean_pss_kb": 1024,
            "mean_pss_mib": 1.0,
        },
        "containerd_shim": {
            "count": 1,
            "pss_kb": 2048,
            "pss_mib": 2.0,
            "mean_pss_kb": 2048,
            "mean_pss_mib": 2.0,
        },
        "conmon": {
            "count": 0,
            "pss_kb": 0,
            "pss_mib": 0.0,
            "mean_pss_kb": 0,
            "mean_pss_mib": 0.0,
        },
        "podman": {
            "count": 0,
            "pss_kb": 0,
            "pss_mib": 0.0,
            "mean_pss_kb": 0,
            "mean_pss_mib": 0.0,
        },
        "passt": {
            "count": 0,
            "pss_kb": 0,
            "pss_mib": 0.0,
            "mean_pss_kb": 0,
            "mean_pss_mib": 0.0,
        },
        "slirp4netns": {
            "count": 0,
            "pss_kb": 0,
            "pss_mib": 0.0,
            "mean_pss_kb": 0,
            "mean_pss_mib": 0.0,
        },
        "dockerd": {
            "count": 0,
            "pss_kb": 0,
            "pss_mib": 0.0,
            "mean_pss_kb": 0,
            "mean_pss_mib": 0.0,
        },
        "other_runtime": {
            "count": 0,
            "pss_kb": 0,
            "pss_mib": 0.0,
            "mean_pss_kb": 0,
            "mean_pss_mib": 0.0,
        },
    }
    assert summary["overhead"]["runtime_process_top"] == [
        {
            "pid": 102,
            "comm": "containerd-shim-runc-v2",
            "cmdline": "/usr/bin/containerd-shim-runc-v2 -namespace k8s.io",
            "group": "containerd_shim",
            "pss_kb": 2048,
            "pss_mib": 2.0,
        },
        {
            "pid": 101,
            "comm": "containerd",
            "cmdline": "/usr/bin/containerd",
            "group": "containerd",
            "pss_kb": 1024,
            "pss_mib": 1.0,
        },
    ]


def test_runtime_attribution_for_podman_lane_classifies_passt_separately(tmp_path: Path) -> None:
    snap = tmp_path
    raw = snap / "raw"
    raw.mkdir(parents=True, exist_ok=True)

    (snap / "meta.json").write_text(
        json.dumps({"mode": "k1s", "label": "t", "backend": "podman", "engine_filter": "podman"})
    )
    (raw / "ps_before.txt").write_text(
        "PID PPID COMMAND RSS\n"
        "201 1 podman 0\n"
        "202 1 conmon 0\n"
        "203 1 passt.avx2 0\n"
        "204 1 containerd 0\n"
        "205 1 dockerd 0\n"
    )
    (raw / "ps_scan_before.txt").write_text(
        "PID PPID COMMAND COMMAND\n"
        "201 1 podman /usr/bin/podman system service\n"
        "202 1 conmon /usr/bin/conmon --api-version 1\n"
        "203 1 passt.avx2 /usr/bin/passt.avx2 --run-for podman\n"
        "204 1 containerd /usr/bin/containerd\n"
        "205 1 dockerd /usr/bin/dockerd\n"
    )
    (raw / "smaps_201_podman.txt").write_text("Pss:               1024 kB\n")
    (raw / "smaps_202_conmon.txt").write_text("Pss:                512 kB\n")
    (raw / "smaps_203_passt.avx2.txt").write_text("Pss:               2048 kB\n")
    (raw / "smaps_204_containerd.txt").write_text("Pss:               4096 kB\n")
    (raw / "smaps_205_dockerd.txt").write_text("Pss:               8192 kB\n")

    summary = MA.aggregate(snap)

    assert summary["overhead"]["pss_kb_runtime"] == 3584
    assert summary["overhead"]["runtime_process_groups"] == {
        "containerd": 0,
        "containerd_shim": 0,
        "conmon": 512,
        "podman": 1024,
        "passt": 2048,
        "slirp4netns": 0,
        "dockerd": 0,
        "other_runtime": 0,
    }
    assert summary["overhead"]["runtime_process_group_stats"] == {
        "containerd": {
            "count": 0,
            "pss_kb": 0,
            "pss_mib": 0.0,
            "mean_pss_kb": 0,
            "mean_pss_mib": 0.0,
        },
        "containerd_shim": {
            "count": 0,
            "pss_kb": 0,
            "pss_mib": 0.0,
            "mean_pss_kb": 0,
            "mean_pss_mib": 0.0,
        },
        "conmon": {
            "count": 1,
            "pss_kb": 512,
            "pss_mib": 0.5,
            "mean_pss_kb": 512,
            "mean_pss_mib": 0.5,
        },
        "podman": {
            "count": 1,
            "pss_kb": 1024,
            "pss_mib": 1.0,
            "mean_pss_kb": 1024,
            "mean_pss_mib": 1.0,
        },
        "passt": {
            "count": 1,
            "pss_kb": 2048,
            "pss_mib": 2.0,
            "mean_pss_kb": 2048,
            "mean_pss_mib": 2.0,
        },
        "slirp4netns": {
            "count": 0,
            "pss_kb": 0,
            "pss_mib": 0.0,
            "mean_pss_kb": 0,
            "mean_pss_mib": 0.0,
        },
        "dockerd": {
            "count": 0,
            "pss_kb": 0,
            "pss_mib": 0.0,
            "mean_pss_kb": 0,
            "mean_pss_mib": 0.0,
        },
        "other_runtime": {
            "count": 0,
            "pss_kb": 0,
            "pss_mib": 0.0,
            "mean_pss_kb": 0,
            "mean_pss_mib": 0.0,
        },
    }
    assert summary["overhead"]["runtime_process_top"] == [
        {
            "pid": 203,
            "comm": "passt.avx2",
            "cmdline": "/usr/bin/passt.avx2 --run-for podman",
            "group": "passt",
            "pss_kb": 2048,
            "pss_mib": 2.0,
        },
        {
            "pid": 201,
            "comm": "podman",
            "cmdline": "/usr/bin/podman system service",
            "group": "podman",
            "pss_kb": 1024,
            "pss_mib": 1.0,
        },
        {
            "pid": 202,
            "comm": "conmon",
            "cmdline": "/usr/bin/conmon --api-version 1",
            "group": "conmon",
            "pss_kb": 512,
            "pss_mib": 0.5,
        },
    ]
