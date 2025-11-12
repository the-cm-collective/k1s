#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import math
import re
import sys
from pathlib import Path
import os
from typing import Dict, List, Tuple


def scenario_name(row: Dict[str, str]) -> str:
    mode = (row.get("mode") or "").lower().strip() or "?"
    backend = (row.get("backend") or "").lower().strip() or "?"
    label = row.get("label", "")
    root_tag = "rootless" if "+rootless+" in label else ("priv" if "+priv+" in label else "?")
    if mode == "k1s" and backend == "podman" and root_tag == "rootless":
        return "k1s rootless"
    if mode == "k1s" and backend == "podman" and root_tag == "priv":
        return "k1s rootful"
    if mode == "k1s" and backend == "docker":
        return "k1nd"
    if mode == "k3s":
        return "k3d"
    return f"{mode} {backend}"


def stage_name(label: str) -> str:
    l = label or ""
    if l.endswith("-idle"):
        return "idle"
    m = re.search(r"-pods-(\d+)$", l)
    if m:
        return f"pods-{m.group(1)}"
    m = re.search(r"-rollout-(\d+)-(during|post)$", l)
    if m:
        return f"rollout-{m.group(1)}-{m.group(2)}"
    return "other"


def to_mib(val: str | int | float, *, kib: bool = False) -> float:
    try:
        v = float(val or 0)
    except Exception:
        v = 0.0
    if kib:
        return v / 1024.0
    return v / (1024.0 * 1024.0)


# Material-ish flat colors and dark grey background for better readability
PALETTE = {
    # Greens
    "k1s rootless": "#66BB6A",  # Green 400
    "k1s rootful": "#2E7D32",   # Green 800
    # Blue / Amber
    "k1nd": "#42A5F5",          # Blue 400
    "k3d": "#FFB300",           # Amber 600
}

BG_DARK = "#263238"   # Blue Grey 900
FG_LIGHT = "#ECEFF1"  # Blue Grey 50
GRID_COLOR = "#546E7A"  # Blue Grey 600


def latest_per_scenario_stage(rows: List[Dict[str, str]]) -> Dict[Tuple[str, str], Dict[str, str]]:
    latest: Dict[Tuple[str, str], Dict[str, str]] = {}
    for r in rows:
        sc = scenario_name(r)
        st = stage_name(r.get("label", ""))
        if st == "other":
            continue
        key = (sc, st)
        prev = latest.get(key)
        if prev is None or str(r.get("timestamp", "")) > str(prev.get("timestamp", "")):
            latest[key] = r
    return latest


def ensure_matplotlib():
    try:
        # Force non-GUI backend to avoid Qt/DBus noise under sudo/CI
        import matplotlib as mpl  # type: ignore

        try:
            mpl.use("Agg", force=True)
        except Exception:
            pass
        import matplotlib.pyplot as plt  # type: ignore

        return plt
    except Exception:
        print("matplotlib not available; install with: pip install matplotlib", file=sys.stderr)
        print("skipping plot generation; combined CSV remains available", file=sys.stderr)
        return None


def bar_labels(ax):
    try:
        ax.bar_label(ax.containers[-1], fmt="%.1f", padding=2, fontsize=8)
    except Exception:
        pass


def plot_grouped_bars(
    plt,
    outdir: Path,
    stage: str,
    metric: str,
    values: List[Tuple[str, float]],
    *,
    ylabel: str,
    ylim: Tuple[float, float] | None,
):
    if not values:
        return
    # Sort low→high for readability
    values = sorted(values, key=lambda x: x[1])
    labels = [v[0] for v in values]
    data = [v[1] for v in values]
    colors = [PALETTE.get(s, "#94a3b8") for s in labels]
    fig, ax = plt.subplots(figsize=(11, 4.5))
    bars = ax.bar(labels, data, color=colors)
    # Highlight winner
    if bars:
        bars[0].set_edgecolor("#10b981")
        bars[0].set_linewidth(2)
    bar_labels(ax)
    ax.set_ylabel(ylabel)
    ax.set_title(f"{metric} — {stage}")
    ax.grid(axis="y", linestyle=":", alpha=0.4)
    plt.xticks(rotation=30, ha="right")
    if ylim:
        ax.set_ylim(0, max(ylim[1], max(data) * 1.1))
    plt.tight_layout()
    fname = outdir / f"comparison_{metric.lower().replace(' ', '_')}_{stage}.png"
    try:
        plt.savefig(fname, dpi=120)
        try:
            plt.savefig(fname.with_suffix(".svg"))
        except Exception:
            pass
    except PermissionError:
        alt = Path("charts-user")
        alt.mkdir(parents=True, exist_ok=True)
        alt_name = alt / fname.name
        print(f"[plot] permission denied for {fname}; writing to {alt_name}", file=sys.stderr)
        plt.savefig(alt_name, dpi=120)
        try:
            plt.savefig(alt_name.with_suffix(".svg"))
        except Exception:
            pass
    plt.close()


def plot_per_pod_scaling(plt, outdir: Path, rows: List[Dict[str, str]]):
    # Build latest per scenario per replica count
    by_scn: Dict[str, Dict[int, float]] = {}
    for r in rows:
        st = stage_name(r.get("label", ""))
        if not st.startswith("pods-"):
            continue
        n = int(st.split("-", 1)[1])
        sc = scenario_name(r)
        val = (int(r.get("app_mem_bytes") or 0) / max(1, n)) / (1024.0 * 1024.0)
        cur = by_scn.setdefault(sc, {})
        # Keep latest by timestamp (lexicographic compare on YYYYMMDD-HHMMSS)
        prev_ts = str(cur.get(-n, ""))
        ts = str(r.get("timestamp", ""))
        if ts >= prev_ts:
            cur[n] = val
            cur[-n] = ts
    if not by_scn:
        return
    fig, ax = plt.subplots(figsize=(11, 4.5))
    for sc, d in by_scn.items():
        points = sorted([(k, v) for k, v in d.items() if k > 0], key=lambda x: x[0])
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        ax.plot(xs, ys, marker="o", color=PALETTE.get(sc, "#94a3b8"), label=sc)
    ax.set_xlabel("Replicas")
    ax.set_ylabel("Per‑pod app mem (MiB)")
    ax.set_title("Per‑pod Scaling by Scenario")
    ax.grid(axis="both", linestyle=":", alpha=0.4)
    ax.legend(frameon=False)
    plt.tight_layout()
    out = outdir / "per_pod_scaling.png"
    try:
        plt.savefig(out, dpi=120)
        try:
            plt.savefig(out.with_suffix(".svg"))
        except Exception:
            pass
    except PermissionError:
        alt = Path("charts-user")
        alt.mkdir(parents=True, exist_ok=True)
        alt_out = alt / out.name
        print(f"[plot] permission denied for {out}; writing to {alt_out}", file=sys.stderr)
        plt.savefig(alt_out, dpi=120)
        try:
            plt.savefig(alt_out.with_suffix(".svg"))
        except Exception:
            pass
    plt.close()


def plot_rollout_pairs(plt, outdir: Path, latest_map: Dict[Tuple[str, str], Dict[str, str]], replicas: int | None):
    # Determine replicas to target: use provided or the most common N available
    ns = []
    for (_sc, st) in latest_map.keys():
        m = re.match(r"rollout-(\d+)-(during|post)$", st)
        if m:
            ns.append(int(m.group(1)))
    if not ns:
        return
    target = replicas if replicas and replicas in ns else max(set(ns), key=ns.count)
    during_vals: List[Tuple[str, float]] = []
    post_vals: List[Tuple[str, float]] = []
    for sc in ["k1s rootless", "k1s rootful", "k1nd", "k3d"]:
        r_d = latest_map.get((sc, f"rollout-{target}-during"))
        r_p = latest_map.get((sc, f"rollout-{target}-post"))
        if not r_d or not r_p:
            continue
        # Use derived Control‑plane PSS consistently
        during_vals.append((sc, _cp_pss_mib_derived(r_d)))
        post_vals.append((sc, _cp_pss_mib_derived(r_p)))
    if not during_vals:
        return
    # Align order by scenario
    order = [s for s, _ in sorted(during_vals, key=lambda x: x[1])]
    dv = [v for s, v in sorted(during_vals, key=lambda x: order.index(x[0]))]
    pv = [dict(post_vals)[s] for s in order]
    colors = [PALETTE.get(s, "#94a3b8") for s in order]
    import numpy as np  # type: ignore

    x = np.arange(len(order))
    width = 0.36
    fig, ax = plt.subplots(figsize=(11, 4.5))
    b1 = ax.bar(x - width / 2, dv, width, label="during", color=colors, alpha=0.9)
    b2 = ax.bar(x + width / 2, pv, width, label="post", color=colors, alpha=0.55)
    ax.set_xticks(x)
    ax.set_xticklabels(order, rotation=30, ha="right")
    ax.set_ylabel("Control‑plane PSS (MiB)")
    ax.set_title(f"Rollout {target} — During vs Post")
    ax.legend(frameon=False)
    ax.grid(axis="y", linestyle=":", alpha=0.4)
    plt.tight_layout()
    out = outdir / "rollout_pairs.png"
    try:
        plt.savefig(out, dpi=120)
        try:
            plt.savefig(out.with_suffix(".svg"))
        except Exception:
            pass
    except PermissionError:
        alt = Path("charts-user")
        alt.mkdir(parents=True, exist_ok=True)
        alt_out = alt / out.name
        print(f"[plot] permission denied for {out}; writing to {alt_out}", file=sys.stderr)
        plt.savefig(alt_out, dpi=120)
        try:
            plt.savefig(alt_out.with_suffix(".svg"))
        except Exception:
            pass
    plt.close()


def _cp_pss_mib_derived(r: Dict[str, str]) -> float:
    """Derive Control‑Plane PSS (MiB) consistently across modes.

    - k3s: k3s process PSS only (uses inner measurement when exported).
    - k1s: controller + ingress PSS.
    Falls back to historical aggregate if structured fields are missing.
    """
    mode = str(r.get("mode", "")).lower()
    try:
        if mode == "k3s":
            v = r.get("k3s_control_plane_pss_kb")
            # Treat missing or zero as unavailable; fall back to legacy aggregate
            if v not in (None, "", "0", 0):
                return to_mib(v, kib=True)
        # k1s and others: controller + ingress
        c = r.get("controller_pss_kb")
        i = r.get("ingress_pss_kb")
        c_val = int(c or 0)
        i_val = int(i or 0)
        if (c_val > 0) or (i_val > 0):
            return to_mib(c_val + i_val, kib=True)
    except Exception:
        pass
    # Final fallback: legacy field from combined CSV
    return to_mib(r.get("control_plane_pss_kb", "0"), kib=True)


def plot_matrix_heatmap(plt, outdir: Path, latest_map: Dict[Tuple[str, str], Dict[str, str]]):
    scenarios = ["k1s rootless", "k1s rootful", "k1nd", "k3d"]
    stages = sorted({st for (_sc, st) in latest_map.keys()})
    if not stages:
        return
    import numpy as np  # type: ignore

    metrics = [
        ("CP PSS", lambda r: _cp_pss_mib_derived(r)),
        ("App", lambda r: to_mib(r.get("app_mem_bytes", "0"))),
        (
            "HostSys",
            lambda r: to_mib(
                r.get("host_system_cgroups_bytes")
                if r.get("host_system_cgroups_bytes") is not None
                else r.get("system_mem_bytes", "0")
            ),
        ),
        ("MemAvailΔ", lambda r: to_mib(r.get("mem_available_delta_bytes", "0"))),
    ]
    fig, axes = plt.subplots(2, 2, figsize=(12, 8))
    axes = axes.flatten()
    for idx, (mtitle, ex) in enumerate(metrics):
        ax = axes[idx]
        data = []
        for st in stages:
            row = []
            for sc in scenarios:
                r = latest_map.get((sc, st))
                row.append(ex(r) if r else math.nan)
            data.append(row)
        arr = np.array(data, dtype=float)
        # Normalize to 0..1 across all values in this metric for color mapping
        finite = arr[np.isfinite(arr)]
        if finite.size:
            mn, mx = float(np.nanmin(arr)), float(np.nanmax(arr))
            denom = (mx - mn) or 1.0
            norm = (arr - mn) / denom
        else:
            norm = arr
        # Use new colormap accessor (avoids deprecation warnings)
        try:
            cmap = plt.colormaps.get_cmap("RdYlGn_r")  # matplotlib >=3.7
        except Exception:
            cmap = plt.cm.get_cmap("RdYlGn_r")  # fallback
        ax.imshow(norm, cmap=cmap, aspect="auto")
        ax.set_title(mtitle)
        ax.set_yticks(range(len(stages)))
        ax.set_yticklabels(stages)
        ax.set_xticks(range(len(scenarios)))
        ax.set_xticklabels(scenarios, rotation=30, ha="right")
        # Overlay values
        for i in range(len(stages)):
            for j in range(len(scenarios)):
                val = arr[i, j]
                if not math.isnan(val):
                    ax.text(j, i, f"{val:.1f}", va="center", ha="center", fontsize=8)
    plt.tight_layout()
    out = outdir / "matrix_heatmap.png"
    try:
        plt.savefig(out, dpi=120)
        try:
            plt.savefig(out.with_suffix(".svg"))
        except Exception:
            pass
    except PermissionError:
        alt = Path("charts-user")
        alt.mkdir(parents=True, exist_ok=True)
        alt_out = alt / out.name
        print(f"[plot] permission denied for {out}; writing to {alt_out}", file=sys.stderr)
        plt.savefig(alt_out, dpi=120)
        try:
            plt.savefig(alt_out.with_suffix(".svg"))
        except Exception:
            pass
    plt.close()


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Generate benchmark charts from combined CSV",
        prog="plot_overhead.py",
    )
    parser.add_argument("csv", help="path to combined/combined.csv")
    parser.add_argument("outdir", nargs="?", default="charts", help="output directory")
    parser.add_argument(
        "--stages",
        default="",
        help="comma-separated subset of stages to render (e.g., idle,pods-5,rollout-5-post)",
    )
    parser.add_argument(
        "--rollout-replicas",
        type=int,
        default=None,
        help="target replicas N for rollout pairs; defaults to most common",
    )
    parser.add_argument(
        "--latest",
        type=int,
        default=int(os.getenv("PLOT_LATEST", "60") or 60),
        help="limit legacy bar charts to the last N rows (default from $PLOT_LATEST or 60)",
    )
    args = parser.parse_args(argv[1:])

    csv_path = Path(args.csv)
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    # If charts directory exists but isn't writable (e.g., root-owned from sudo run),
    # fall back to a user-writable sibling directory.
    try:
        probe = outdir / ".__write_probe__"
        with probe.open("w") as _fh:
            _fh.write("ok")
        probe.unlink(missing_ok=True)  # type: ignore[arg-type]
    except Exception:
        alt = Path("charts-user")
        alt.mkdir(parents=True, exist_ok=True)
        print(f"[plot] '{outdir}' not writable; falling back to '{alt}'", file=sys.stderr)
        outdir = alt

    rows: List[Dict[str, str]] = []
    with csv_path.open("r", encoding="utf-8", errors="ignore") as fh:
        cr = csv.DictReader(fh)
        for r in cr:
            rows.append(r)
    if not rows:
        print("no rows in combined csv", file=sys.stderr)
        return 1

    plt = ensure_matplotlib()
    if plt is None:
        return 0

    # Preserve legacy charts for continuity (now using derived CP PSS)
    # Limit to last N rows to avoid unreadably wide charts
    legacy_rows = rows[-args.latest :] if args.latest and len(rows) > args.latest else rows
    labels = [r.get("label", "") for r in legacy_rows]
    scenarios = [scenario_name(r) for r in legacy_rows]
    pss = [_cp_pss_mib_derived(r) for r in legacy_rows]
    colors = [PALETTE.get(sc, "#94a3b8") for sc in scenarios]
    fig, ax = plt.subplots(figsize=(10, 4))
    ax.set_facecolor(BG_DARK)
    fig.patch.set_facecolor(BG_DARK)
    bars = ax.bar(labels, pss, color=colors, edgecolor="#000000", linewidth=0)
    ax.set_ylabel("Control-plane PSS (MiB)", color=FG_LIGHT)
    ax.tick_params(colors=FG_LIGHT)
    ax.spines["bottom"].set_color(FG_LIGHT)
    ax.spines["left"].set_color(FG_LIGHT)
    ax.grid(axis="y", linestyle=":", color=GRID_COLOR, alpha=0.5)
    ax.set_axisbelow(True)
    for lbl in ax.get_xticklabels():
        lbl.set_rotation(45)
        lbl.set_ha("right")
        lbl.set_color(FG_LIGHT)
        lbl.set_fontsize(8)
    # Add a compact legend for scenario colors
    try:
        import matplotlib.patches as mpatches  # type: ignore
        handles = [
            mpatches.Patch(color=PALETTE.get(sc, "#94a3b8"), label=sc)
            for sc in ["k1s rootless", "k1s rootful", "k1nd", "k3d"]
        ]
        ax.legend(handles=handles, frameon=False, labelcolor=FG_LIGHT, facecolor=BG_DARK, loc="upper left")
    except Exception:
        pass
    fig.tight_layout()
    try:
        plt.savefig(outdir / "control_plane_pss.png", dpi=120)
    except PermissionError:
        alt = Path("charts-user")
        alt.mkdir(parents=True, exist_ok=True)
        plt.savefig(alt / "control_plane_pss.png", dpi=120)
    plt.close()

    sys_mem = [
        to_mib((r.get("host_system_cgroups_bytes") or r.get("system_mem_bytes") or 0))
        for r in legacy_rows
    ]
    fig, ax = plt.subplots(figsize=(10, 4))
    ax.set_facecolor(BG_DARK)
    fig.patch.set_facecolor(BG_DARK)
    bars = ax.bar(labels, sys_mem, color=colors, edgecolor="#000000", linewidth=0)
    ax.set_ylabel("System cgroups (MiB)", color=FG_LIGHT)
    ax.tick_params(colors=FG_LIGHT)
    ax.spines["bottom"].set_color(FG_LIGHT)
    ax.spines["left"].set_color(FG_LIGHT)
    ax.grid(axis="y", linestyle=":", color=GRID_COLOR, alpha=0.5)
    ax.set_axisbelow(True)
    for lbl in ax.get_xticklabels():
        lbl.set_rotation(45)
        lbl.set_ha("right")
        lbl.set_color(FG_LIGHT)
        lbl.set_fontsize(8)
    try:
        import matplotlib.patches as mpatches  # type: ignore
        handles = [
            mpatches.Patch(color=PALETTE.get(sc, "#94a3b8"), label=sc)
            for sc in ["k1s rootless", "k1s rootful", "k1nd", "k3d"]
        ]
        ax.legend(handles=handles, frameon=False, labelcolor=FG_LIGHT, facecolor=BG_DARK, loc="upper left")
    except Exception:
        pass
    fig.tight_layout()
    try:
        plt.savefig(outdir / "system_cgroups.png", dpi=120)
    except PermissionError:
        alt = Path("charts-user")
        alt.mkdir(parents=True, exist_ok=True)
        plt.savefig(alt / "system_cgroups.png", dpi=120)
    plt.close()

    # New comparative charts
    latest_map = latest_per_scenario_stage(rows)
    # Determine stages to render
    stages_all = sorted({st for (_sc, st) in latest_map.keys()})
    if args.stages:
        stages = [s for s in args.stages.split(",") if s in stages_all]
    else:
        stages = stages_all

    # Precompute metric ranges for consistent y-limits
    def collect_metric(stage_filter: str, ex) -> List[Tuple[str, float]]:
        vals: List[Tuple[str, float]] = []
        for sc in ["k1s rootless", "k1s rootful", "k1nd", "k3d"]:
            r = latest_map.get((sc, stage_filter))
            if r:
                val = ex(r)
                if val is not None and not (isinstance(val, float) and (val != val)):
                    vals.append((sc, val))
        return vals

    # Metric extractors
    def ex_cp(r):
        return _cp_pss_mib_derived(r)
    ex_app = lambda r: to_mib(r.get("app_mem_bytes", "0"))
    ex_host = lambda r: to_mib(
        r.get("host_system_cgroups_bytes")
        if r.get("host_system_cgroups_bytes") is not None
        else r.get("system_mem_bytes", "0")
    )
    ex_mad = lambda r: to_mib(r.get("mem_available_delta_bytes", "0"))

    # Compute global max per metric across selected stages
    def max_of(metric_ex) -> float:
        m = 0.0
        for st in stages:
            for _sc, v in collect_metric(st, metric_ex):
                m = max(m, v)
        return m

    ylim_cp = (0.0, max_of(ex_cp))
    ylim_app = (0.0, max_of(ex_app))
    ylim_host = (0.0, max_of(ex_host))
    ylim_mad = (0.0, max_of(ex_mad))

    for st in stages:
        plot_grouped_bars(
            plt,
            outdir,
            st,
            "control_plane_pss",
            collect_metric(st, ex_cp),
            ylabel="Control‑plane PSS (MiB)",
            ylim=ylim_cp,
        )
        plot_grouped_bars(
            plt,
            outdir,
            st,
            "app_cgroups",
            collect_metric(st, ex_app),
            ylabel="App Cgroups (MiB)",
            ylim=ylim_app,
        )
        plot_grouped_bars(
            plt,
            outdir,
            st,
            "host_system_cgroups",
            collect_metric(st, ex_host),
            ylabel="Host System Cgroups (MiB)",
            ylim=ylim_host,
        )
        plot_grouped_bars(
            plt,
            outdir,
            st,
            "memavail_delta",
            collect_metric(st, ex_mad),
            ylabel="MemAvail Δ (MiB)",
            ylim=ylim_mad,
        )

    # Per-pod scaling lines
    plot_per_pod_scaling(plt, outdir, rows)

    # Rollout during vs post pairs
    plot_rollout_pairs(plt, outdir, latest_map, args.rollout_replicas)

    # Matrix heatmap across metrics
    plot_matrix_heatmap(plt, outdir, latest_map)

    print(f"wrote charts to {outdir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
