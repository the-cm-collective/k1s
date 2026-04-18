#!/usr/bin/env python3
# ruff: noqa
from __future__ import annotations

import argparse
import csv
import math
import re
import sys
from pathlib import Path
import os
from typing import Dict, List, Tuple
import os


def scenario_name(row: Dict[str, str]) -> str:
    """Classify a snapshot row into a scenario name used by charts.

    Prefer explicit metadata fields over brittle label token parsing.
    Fallbacks preserve historical behavior.
    """
    mode = (row.get("mode") or "").lower().strip() or "?"
    backend = (row.get("backend") or "").lower().strip() or "?"
    # Rootless from metadata when available; tolerate various truthy strings
    raw_rootless = str(row.get("rootless") or "").strip().lower()
    is_rootless = raw_rootless in ("1", "true", "yes", "y")
    # Historical fallback for older snapshots that encode rootless in label tokens
    if not raw_rootless:
        label = row.get("label", "") or ""
        if "+rootless+" in label:
            is_rootless = True
        elif "+priv+" in label:
            is_rootless = False

    if mode == "k1s" and backend == "podman":
        return "k1s rootless" if is_rootless else "k1s rootful"
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
    m = re.search(r"-rollout-(\d+)-(during(?:-warm)?|post)$", l)
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


def display_label(row: Dict[str, str]) -> str:
    """Return a label string augmented with '+<oci>+' when missing.

    This avoids having to mutate snapshot metadata (which may be root-owned),
    and keeps legacy charts informative about the runtime (crun/runc).
    """
    label = row.get("label", "") or ""
    oci = str(row.get("oci_runtime") or "").strip().lower()
    if not oci or f"+{oci}+" in label:
        return label
    # Prefer inserting after '+<backend>+' if present
    backend = (row.get("backend") or "").strip().lower()
    token_b = f"+{backend}+" if backend else None
    if token_b and token_b in label:
        return label.replace(token_b, f"{token_b}{oci}+")
    # Fallback: insert before stage suffixes like '-idle', '-pods-N', or rollout
    import re

    m = (
        re.search(r"(-idle)$", label)
        or re.search(r"(-pods-\d+)$", label)
        or re.search(r"(-rollout-\d+-(during(?:-warm)?|post))$", label)
    )
    if m:
        start, _end = m.span(1)
        return f"{label[:start]}+{oci}+{label[start:]}"
    # Last resort: append a compact suffix
    return f"{label}+{oci}"


def _pretty_ts(ts: str) -> str:
    # Expect YYYYMMDD-HHMMSS; return 'MM-DD HH:MM'
    if ts and len(ts) >= 15 and ts[8] == "-":
        try:
            return f"{ts[4:6]}-{ts[6:8]} {ts[9:11]}:{ts[11:13]}"
        except Exception:
            pass
    return (ts or "").strip()


def compact_label(row: Dict[str, str]) -> str:
    """Compact timeline label: 'MM-DD HH:MM <stage>'.

    Keeps the key context for per-scenario timelines without overflowing.
    """
    st = stage_name(row.get("label", ""))
    return f"{_pretty_ts(str(row.get('timestamp','')))} {st}"


# Material-ish flat colors and dark grey background for better readability
PALETTE = {
    # Greens
    "k1s rootless": "#66BB6A",  # Green 400
    "k1s rootful": "#2E7D32",  # Green 800
    # Blue / Amber
    "k1nd": "#42A5F5",  # Blue 400
    "k3d": "#FFB300",  # Amber 600
}

BG_DARK = "#263238"  # Blue Grey 900
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
    except Exception as exc:
        print("matplotlib not available; install with: pip install matplotlib", file=sys.stderr)
        print(f"matplotlib import error: {exc}", file=sys.stderr)
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
    # Legend optional (off by default)
    if str(os.getenv("PLOT_SHOW_LEGEND", "0")).lower() not in ("0", "false", "no", ""):
        ax.legend(
            loc="upper center", bbox_to_anchor=(0.5, 1.18), ncol=max(1, len(by_scn)), frameon=False
        )
    plt.tight_layout(rect=[0, 0, 1, 0.9])
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


def plot_rollout_pairs(
    plt,
    outdir: Path,
    latest_map: Dict[Tuple[str, str], Dict[str, str]],
    replicas: int | None,
    scenarios: List[str],
):
    # Determine replicas to target: use provided or the most common N available
    ns = []
    for _sc, st in latest_map.keys():
        m = re.match(r"rollout-(\d+)-(during(?:-warm)?|post)$", st)
        if m:
            ns.append(int(m.group(1)))
    if not ns:
        return
    target = replicas if replicas and replicas in ns else max(set(ns), key=ns.count)
    during_vals: List[Tuple[str, float]] = []
    during_warm_vals: List[Tuple[str, float]] = []
    post_vals: List[Tuple[str, float]] = []
    for sc in scenarios:
        r_d = latest_map.get((sc, f"rollout-{target}-during"))
        r_dw = latest_map.get((sc, f"rollout-{target}-during-warm"))
        r_p = latest_map.get((sc, f"rollout-{target}-post"))
        if not r_d or not r_p:
            continue
        # Use derived Control‑plane PSS consistently
        during_vals.append((sc, _cp_pss_mib_derived(r_d)))
        if r_dw:
            during_warm_vals.append((sc, _cp_pss_mib_derived(r_dw)))
        post_vals.append((sc, _cp_pss_mib_derived(r_p)))
    if not during_vals:
        return
    # Align order by scenario
    order = [s for s, _ in sorted(during_vals, key=lambda x: x[1])]
    dv = [v for s, v in sorted(during_vals, key=lambda x: order.index(x[0]))]
    dw_map = dict(during_warm_vals)
    has_warm = all(s in dw_map for s in order)
    dwv = [dw_map.get(s, dict(post_vals)[s]) for s in order]
    pv = [dict(post_vals)[s] for s in order]
    colors = [PALETTE.get(s, "#94a3b8") for s in order]
    import numpy as np  # type: ignore

    x = np.arange(len(order))
    width = 0.24 if has_warm else 0.36
    fig, ax = plt.subplots(figsize=(11, 4.5))
    ax.bar(x - width, dv, width, label="during", color=colors, alpha=0.9)
    if has_warm:
        ax.bar(x, dwv, width, label="during-warm", color=colors, alpha=0.72)
        ax.bar(x + width, pv, width, label="post", color=colors, alpha=0.55)
    else:
        ax.bar(x + width / 2, pv, width, label="post", color=colors, alpha=0.55)
    ax.set_xticks(x)
    ax.set_xticklabels(order, rotation=30, ha="right")
    ax.set_ylabel("Control‑plane PSS (MiB)")
    ax.set_title(f"Rollout {target} — During vs During-Warm vs Post")
    if str(os.getenv("PLOT_SHOW_LEGEND", "0")).lower() not in ("0", "false", "no", ""):
        ax.legend(
            loc="upper center",
            bbox_to_anchor=(0.5, 1.18),
            ncol=(3 if has_warm else 2),
            frameon=False,
        )
    ax.grid(axis="y", linestyle=":", alpha=0.4)
    plt.tight_layout(rect=[0, 0, 1, 0.9])
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


def _memavail_delta_mib(r: Dict[str, str]) -> float:
    """Return MemAvail Δ in MiB with backfill from before/after if needed."""
    try:
        raw = float(r.get("mem_available_delta_bytes", 0) or 0)
    except Exception:
        raw = 0.0
    if raw == 0.0:
        try:
            before = float(r.get("mem_available_before_bytes", 0) or 0)
            after = float(r.get("mem_available_after_bytes", 0) or 0)
            raw = after - before
        except Exception:
            raw = 0.0
    return raw / (1024.0 * 1024.0)


def plot_matrix_heatmap(
    plt,
    outdir: Path,
    latest_map: Dict[Tuple[str, str], Dict[str, str]],
    scenarios: List[str],
):
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

    # Preserve legacy timeline charts but split per scenario (Option B)
    # Limit to last N rows to avoid unreadably wide charts
    legacy_rows = rows[-args.latest :] if args.latest and len(rows) > args.latest else rows

    def scenario_key_to_suffix(sc: str) -> str:
        return sc.replace(" ", "_")

    def render_legacy_for(sc_filter: str):
        subset = [r for r in legacy_rows if scenario_name(r) == sc_filter]
        if not subset:
            return
        labels = [compact_label(r) for r in subset]
        pss = [_cp_pss_mib_derived(r) for r in subset]
        color = PALETTE.get(sc_filter, "#94a3b8")
        fig, ax = plt.subplots(figsize=(10, 4))
        ax.set_facecolor(BG_DARK)
        fig.patch.set_facecolor(BG_DARK)
        ax.bar(labels, pss, color=color, edgecolor="#000000", linewidth=0)
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
        # Thin x labels if too dense
        if len(labels) > 18:
            step = max(1, len(labels) // 18)
            for idx, lbl in enumerate(ax.get_xticklabels()):
                if idx % step != 0:
                    lbl.set_visible(False)
        fig.tight_layout(rect=[0, 0, 1, 0.95])
        name = f"control_plane_pss_{scenario_key_to_suffix(sc_filter)}.png"
        try:
            plt.savefig(outdir / name, dpi=120)
        except PermissionError:
            alt = Path("charts-user")
            alt.mkdir(parents=True, exist_ok=True)
            plt.savefig(alt / name, dpi=120)
        plt.close()

        sys_mem = [
            to_mib((r.get("host_system_cgroups_bytes") or r.get("system_mem_bytes") or 0))
            for r in subset
        ]
        fig, ax = plt.subplots(figsize=(10, 4))
        ax.set_facecolor(BG_DARK)
        fig.patch.set_facecolor(BG_DARK)
        ax.bar(labels, sys_mem, color=color, edgecolor="#000000", linewidth=0)
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
        if len(labels) > 18:
            step = max(1, len(labels) // 18)
            for idx, lbl in enumerate(ax.get_xticklabels()):
                if idx % step != 0:
                    lbl.set_visible(False)
        fig.tight_layout(rect=[0, 0, 1, 0.95])
        name = f"system_cgroups_{scenario_key_to_suffix(sc_filter)}.png"
        try:
            plt.savefig(outdir / name, dpi=120)
        except PermissionError:
            alt = Path("charts-user")
            alt.mkdir(parents=True, exist_ok=True)
            plt.savefig(alt / name, dpi=120)
        plt.close()

    # Re-enable k1nd timeline now that recent runs have valid PSS
    for scn in ["k3d", "k1s rootless", "k1s rootful", "k1s cri", "k1nd"]:
        render_legacy_for(scn)

    # New comparative charts
    latest_map = latest_per_scenario_stage(rows)
    # Determine stages to render
    stages_all = sorted({st for (_sc, st) in latest_map.keys()})
    if args.stages:
        stages = [s for s in args.stages.split(",") if s in stages_all]
    else:
        stages = stages_all

    # Metric extractors (reused across charts)
    def ex_cp(r):
        return _cp_pss_mib_derived(r)

    ex_app = lambda r: to_mib(r.get("app_mem_bytes", "0"))
    ex_host = lambda r: to_mib(
        r.get("host_system_cgroups_bytes")
        if r.get("host_system_cgroups_bytes") is not None
        else r.get("system_mem_bytes", "0")
    )
    ex_mad = lambda r: _memavail_delta_mib(r)

    # Per-pod scaling lines
    plot_per_pod_scaling(plt, outdir, rows)

    # Per-pod overhead (derived CP PSS per replica)
    def plot_per_pod_overhead():
        by_scn: Dict[str, Dict[int, float]] = {}
        by_ts: Dict[str, Dict[int, str]] = {}
        for r in rows:
            st = stage_name(r.get("label", ""))
            if not st.startswith("pods-"):
                continue
            try:
                n = int(st.split("-", 1)[1])
            except Exception:
                continue
            sc = scenario_name(r)
            cp = _cp_pss_mib_derived(r)
            if n <= 0:
                continue
            val = cp / n
            ts = str(r.get("timestamp", ""))
            cur_ts = by_ts.setdefault(sc, {}).get(n) or ""
            if ts >= cur_ts:
                by_scn.setdefault(sc, {})[n] = val
                by_ts.setdefault(sc, {})[n] = ts
        if not by_scn:
            return
        fig, ax = plt.subplots(figsize=(11, 4.5))
        for sc, d in by_scn.items():
            pts = sorted((k, v) for k, v in d.items())
            xs = [k for k, _ in pts]
            ys = [v for _, v in pts]
            ax.plot(xs, ys, marker="o", color=PALETTE.get(sc, "#94a3b8"), label=sc)
        ax.set_xlabel("Replicas")
        ax.set_ylabel("Per‑pod Overhead (MiB)")
        ax.set_title("Per‑pod Overhead by Scenario (CP PSS / replicas)")
        ax.grid(axis="both", linestyle=":", alpha=0.4)
        if str(os.getenv("PLOT_SHOW_LEGEND", "0")).lower() not in ("0", "false", "no", ""):
            ax.legend(frameon=False)
        plt.tight_layout()
        out = outdir / "per_pod_overhead.png"
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

    plot_per_pod_overhead()

    # Coverage filter for comparison charts and heatmap
    desired_order = ["k1s rootless", "k1s rootful", "k1s cri", "k1nd", "k3d"]
    metrics_for_cov = [ex_cp, ex_app, ex_host, ex_mad]
    # Compute stages selected
    selected_stages = stages
    max_per_scn = max(1, len(selected_stages) * len(metrics_for_cov))
    cov: Dict[str, float] = {}
    for sc in desired_order:
        n = 0
        for st in selected_stages:
            r = latest_map.get((sc, st))
            if not r:
                continue
            for ex in metrics_for_cov:
                try:
                    v = ex(r)
                    if v is not None and not (isinstance(v, float) and (v != v)):
                        n += 1
                except Exception:
                    pass
        cov[sc] = n / max_per_scn
    cov_min = float(os.getenv("PLOT_COVERAGE_MIN") or os.getenv("DOCS_COVERAGE_MIN", "0.8"))
    allowed_scenarios = [sc for sc in desired_order if cov.get(sc, 0.0) >= cov_min]
    if not allowed_scenarios:
        allowed_scenarios = [sc for sc in desired_order if cov.get(sc, 0.0) > 0.0] or desired_order

    # Helper recomputed to honor allowed scenarios
    def collect_metric(stage_filter: str, ex) -> List[Tuple[str, float]]:
        vals: List[Tuple[str, float]] = []
        for sc in allowed_scenarios:
            r = latest_map.get((sc, stage_filter))
            if r:
                val = ex(r)
                if val is not None and not (isinstance(val, float) and (val != val)):
                    vals.append((sc, val))
        return vals

    # Precompute metric ranges for consistent y-limits using filtered scenarios
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

    # Re-render grouped comparison charts using allowed scenarios only
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

    # Rollout during vs post pairs (filtered)
    plot_rollout_pairs(plt, outdir, latest_map, args.rollout_replicas, allowed_scenarios)

    # Matrix heatmap across metrics (filtered)
    plot_matrix_heatmap(plt, outdir, latest_map, allowed_scenarios)

    print(f"wrote charts to {outdir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
