#!/usr/bin/env python3
# ruff: noqa: S603
from __future__ import annotations

import argparse
import json
import math
import random
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFilter, ImageFont
except ModuleNotFoundError:  # pragma: no cover - exercised via CLI smoke
    Image = None
    ImageDraw = None
    ImageFilter = None
    ImageFont = None

BEAT_DURATIONS = (2.0, 3.0, 3.5, 3.5)
DEFAULT_OUTPUT_DIR = Path("state/zone_mesh_outputs")


@dataclass(frozen=True)
class Node:
    id: str
    kind: str
    city: str
    zone: str
    point: tuple[float, float]
    radius: float
    label: str | None = None
    reveal_rank: float = 0.0


@dataclass(frozen=True)
class Link:
    a: str
    b: str
    kind: str
    reveal_rank: float


@dataclass
class ActivityPlan:
    origin: str
    query_targets: list[str]
    remote_targets: list[str]
    warm_targets: list[str]
    inference_targets: list[str]
    query_paths: dict[str, list[str]]
    inference_paths: dict[str, list[str]]
    chunk_groups: list[dict[str, list[str] | str]]
    summary_path: list[str]


@dataclass
class SceneGraph:
    profile: str
    scene_spec: dict
    profile_spec: dict
    nodes: dict[str, Node]
    links: list[Link]
    adjacency: dict[str, set[str]]
    activities: ActivityPlan
    edge_order: dict[tuple[str, str], int]
    node_order: dict[str, int]
    state_outline: list[tuple[float, float]]
    city_points: dict[str, tuple[float, float]]
    dc_ids: dict[str, str]
    phases: list[tuple[str, str]]
    timeline: list[tuple[float, float]]
    width: int
    height: int
    fps: int
    duration_seconds: int
    gif_width: int


def clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


def ease(value: float) -> float:
    value = clamp(value)
    return value * value * (3.0 - 2.0 * value)


def ease_out(value: float) -> float:
    value = clamp(value)
    return 1.0 - (1.0 - value) ** 3


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def mix_points(a: tuple[float, float], b: tuple[float, float], t: float) -> tuple[float, float]:
    return (lerp(a[0], b[0], t), lerp(a[1], b[1], t))


def dist(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def rgba(hex_color: str, alpha: float = 1.0) -> tuple[int, int, int, int]:
    value = hex_color.lstrip("#")
    red = int(value[0:2], 16)
    green = int(value[2:4], 16)
    blue = int(value[4:6], 16)
    return (red, green, blue, int(clamp(alpha) * 255))


def node_key(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a < b else (b, a)


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    _require_pillow()
    candidates = []
    if bold:
        candidates.extend(
            [
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
            ]
        )
    candidates.extend(
        [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/dejavu/DejaVuSans.ttf",
        ]
    )
    for candidate in candidates:
        if Path(candidate).exists():
            return ImageFont.truetype(candidate, size=size)
    return ImageFont.load_default()


def load_specs() -> tuple[dict, dict]:
    base = Path(__file__).resolve().parent
    with (base / "zone_mesh_scene.json").open("r", encoding="utf-8") as handle:
        scene_spec = json.load(handle)
    with (base / "zone_mesh_profiles.json").open("r", encoding="utf-8") as handle:
        profiles = json.load(handle)
    return scene_spec, profiles


def split_count(total: int, corridor: int = 0) -> dict[str, int]:
    remainder = max(total - corridor, 0)
    west = remainder // 2 + remainder % 2
    east = remainder // 2
    return {"west": west, "east": east, "corridor": corridor}


def corridor_allocation(profile_spec: dict) -> dict[str, int]:
    budget = int(profile_spec["links"]["corridor_nodes"])
    counts = profile_spec["counts"]
    corridor = {"pop": 0, "colo": 0, "mixed": 0}
    order = ["pop", "colo", "mixed"]
    while budget > 0:
        changed = False
        for kind in order:
            if corridor[kind] < counts[kind] // 3 + 1:
                corridor[kind] += 1
                budget -= 1
                changed = True
                if budget == 0:
                    break
        if not changed:
            break
    return corridor


def point_in_state(point: tuple[float, float], outline: list[tuple[float, float]]) -> bool:
    x, y = point
    inside = False
    last_index = len(outline) - 1
    for index, current in enumerate(outline):
        x1, y1 = outline[last_index]
        x2, y2 = current
        intersects = (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / ((y2 - y1) or 1e-6) + x1
        if intersects:
            inside = not inside
        last_index = index
    return inside


def clamp_to_canvas(
    point: tuple[float, float], width: int, height: int, margin: int = 70
) -> tuple[float, float]:
    return (
        clamp(point[0], margin, width - margin),
        clamp(point[1], margin, height - margin),
    )


def make_zone_point(
    rng: random.Random,
    city: str,
    zone: str,
    kind: str,
    city_point: tuple[float, float],
    width: int,
    height: int,
    outline: list[tuple[float, float]],
    dc_point: tuple[float, float],
) -> tuple[float, float]:
    if zone == "corridor":
        x = rng.uniform(690, 940)
        y = rng.uniform(300, 560)
        y += math.sin((x - 690.0) / 250.0 * math.pi) * rng.uniform(-30, 30)
        point = (x, y)
        return clamp_to_canvas(point, width, height)

    angle = rng.uniform(0.0, math.tau)
    radial_map = {
        "residence": (160.0, 250.0),
        "business": (65.0, 170.0),
        "colo": (110.0, 220.0),
        "pop": (150.0, 255.0),
        "mixed": (90.0, 215.0),
    }
    inner, outer = radial_map[kind]
    radius = rng.uniform(inner, outer)
    aspect = 0.78 if city == "west" else 0.86
    point = (
        city_point[0] + math.cos(angle) * radius,
        city_point[1] + math.sin(angle) * radius * aspect,
    )
    if kind in {"pop", "colo"}:
        bias = 0.2 if city == "west" else -0.2
        point = (
            point[0] + (dc_point[0] - city_point[0]) * bias,
            point[1] + rng.uniform(-18.0, 18.0),
        )
    point = clamp_to_canvas(point, width, height)
    if point_in_state(point, outline):
        return point
    for _ in range(12):
        step = rng.uniform(0.15, 0.45)
        point = mix_points(point, city_point, step)
        point = clamp_to_canvas(point, width, height)
        if point_in_state(point, outline):
            return point
    return point


def node_radius(kind: str) -> float:
    return {
        "dc": 18.0,
        "residence": 7.0,
        "business": 8.0,
        "colo": 8.5,
        "pop": 8.0,
        "mixed": 7.5,
    }[kind]


def add_link(
    links: dict[tuple[str, str], Link],
    nodes: dict[str, Node],
    a: str,
    b: str,
    kind: str,
) -> None:
    if a == b:
        return
    key = node_key(a, b)
    if key in links:
        return
    reveal_rank = max(nodes[a].reveal_rank, nodes[b].reveal_rank) + 0.02
    links[key] = Link(a=key[0], b=key[1], kind=kind, reveal_rank=reveal_rank)


def nearest_nodes(
    node: Node,
    nodes: Iterable[Node],
    limit: int,
    exclude: set[str] | None = None,
) -> list[Node]:
    exclude = exclude or set()
    ranked = sorted(
        (other for other in nodes if other.id != node.id and other.id not in exclude),
        key=lambda other: dist(node.point, other.point),
    )
    return ranked[:limit]


def shortest_path(adjacency: dict[str, set[str]], start: str, end: str) -> list[str]:
    if start == end:
        return [start]
    frontier = [start]
    previous = {start: None}
    while frontier:
        current = frontier.pop(0)
        for neighbor in adjacency[current]:
            if neighbor in previous:
                continue
            previous[neighbor] = current
            if neighbor == end:
                path = [end]
                while path[-1] != start:
                    path.append(previous[path[-1]])
                path.reverse()
                return path
            frontier.append(neighbor)
    return [start, end]


def path_points(path: list[str], nodes: dict[str, Node]) -> list[tuple[float, float]]:
    return [nodes[node_id].point for node_id in path]


def build_scene(scene_spec: dict, profile_name: str, profile_spec: dict) -> SceneGraph:
    width = int(scene_spec["canvas"]["width"])
    height = int(scene_spec["canvas"]["height"])
    outline = [tuple(point) for point in scene_spec["layout"]["state_outline"]]
    city_points = {
        key: tuple(value["point"]) for key, value in scene_spec["layout"]["cities"].items()
    }
    dc_points = {key: tuple(value["point"]) for key, value in scene_spec["layout"]["dcs"].items()}
    corridor = corridor_allocation(profile_spec)
    counts = profile_spec["counts"]
    rng = random.Random(profile_spec["seed"])  # noqa: S311 - deterministic layout for stable renders

    nodes: dict[str, Node] = {}

    def register(node: Node) -> None:
        nodes[node.id] = node

    dc_ids = {"west": "dc-west", "east": "dc-east"}
    register(
        Node(
            id=dc_ids["west"],
            kind="dc",
            city="west",
            zone="west",
            point=dc_points["west"],
            radius=node_radius("dc"),
            label=scene_spec["layout"]["dcs"]["west"]["label"],
            reveal_rank=0.06,
        )
    )
    register(
        Node(
            id=dc_ids["east"],
            kind="dc",
            city="east",
            zone="east",
            point=dc_points["east"],
            radius=node_radius("dc"),
            label=scene_spec["layout"]["dcs"]["east"]["label"],
            reveal_rank=0.34,
        )
    )

    kind_orders = {"business": 0.11, "mixed": 0.13, "pop": 0.15, "colo": 0.17, "residence": 0.19}
    for kind, total in counts.items():
        split = split_count(total, corridor.get(kind, 0))
        for city in ("west", "east"):
            for index in range(split[city]):
                point = make_zone_point(
                    rng=rng,
                    city=city,
                    zone=city,
                    kind=kind,
                    city_point=city_points[city],
                    width=width,
                    height=height,
                    outline=outline,
                    dc_point=dc_points[city],
                )
                reveal_group = 0.08 if city == "west" else 0.32
                register(
                    Node(
                        id=f"{city}-{kind}-{index:02d}",
                        kind=kind,
                        city=city,
                        zone=city,
                        point=point,
                        radius=node_radius(kind),
                        reveal_rank=reveal_group + kind_orders[kind] + index * 0.002,
                    )
                )
        for index in range(split["corridor"]):
            point = make_zone_point(
                rng=rng,
                city="corridor",
                zone="corridor",
                kind=kind,
                city_point=((city_points["west"][0] + city_points["east"][0]) / 2.0, 410.0),
                width=width,
                height=height,
                outline=outline,
                dc_point=((dc_points["west"][0] + dc_points["east"][0]) / 2.0, 380.0),
            )
            register(
                Node(
                    id=f"corridor-{kind}-{index:02d}",
                    kind=kind,
                    city="corridor",
                    zone="corridor",
                    point=point,
                    radius=node_radius(kind),
                    reveal_rank=0.24 + kind_orders[kind] + index * 0.002,
                )
            )

    links: dict[tuple[str, str], Link] = {}

    corridor_nodes = sorted(
        [node for node in nodes.values() if node.zone == "corridor"],
        key=lambda node: node.point[0],
    )
    if corridor_nodes:
        add_link(links, nodes, dc_ids["west"], corridor_nodes[0].id, "backbone")
        add_link(links, nodes, corridor_nodes[-1].id, dc_ids["east"], "backbone")
        for left, right in zip(corridor_nodes, corridor_nodes[1:], strict=False):
            add_link(links, nodes, left.id, right.id, "backbone")
    add_link(links, nodes, dc_ids["west"], dc_ids["east"], "backbone")

    by_city = {
        city: [node for node in nodes.values() if node.city == city] for city in ("west", "east")
    }

    for node in list(nodes.values()):
        if node.kind == "dc":
            continue
        home_dc = dc_ids["west"] if node.city in {"west", "corridor"} else dc_ids["east"]
        if node.zone == "corridor":
            near_dc = (
                dc_ids["west"]
                if dist(node.point, nodes[dc_ids["west"]].point)
                < dist(node.point, nodes[dc_ids["east"]].point)
                else dc_ids["east"]
            )
            add_link(links, nodes, node.id, near_dc, "backbone")
            if node.kind in {"colo", "pop"}:
                far_dc = dc_ids["east"] if near_dc == dc_ids["west"] else dc_ids["west"]
                add_link(links, nodes, node.id, far_dc, "backbone")
            continue

        same_city_nodes = [
            candidate
            for candidate in by_city[node.city]
            if candidate.id != node.id and candidate.kind != "dc"
        ]
        if node.kind == "residence":
            anchors = [
                candidate
                for candidate in same_city_nodes
                if candidate.kind in {"business", "mixed", "pop", "colo"}
            ]
            target = min(anchors, key=lambda candidate: dist(node.point, candidate.point))
            add_link(links, nodes, node.id, target.id, "access")
        elif node.kind in {"business", "mixed"}:
            add_link(links, nodes, node.id, home_dc, "metro")
            for neighbor in nearest_nodes(node, same_city_nodes, 1, exclude={home_dc}):
                add_link(links, nodes, node.id, neighbor.id, "metro")
        elif node.kind in {"colo", "pop"}:
            add_link(links, nodes, node.id, home_dc, "metro")
            for neighbor in nearest_nodes(node, same_city_nodes, 2, exclude={home_dc}):
                add_link(links, nodes, node.id, neighbor.id, "metro")

    mesh_count = int(profile_spec["links"]["local_mesh_per_node"])
    for city in ("west", "east"):
        candidates = [
            node for node in by_city[city] if node.kind in {"business", "mixed", "colo", "pop"}
        ]
        for node in candidates:
            for neighbor in nearest_nodes(node, candidates, mesh_count):
                add_link(links, nodes, node.id, neighbor.id, "mesh")

    west_corridors = sorted(
        [
            node
            for node in nodes.values()
            if node.city in {"west", "corridor"} and node.kind in {"pop", "colo", "mixed"}
        ],
        key=lambda node: abs(node.point[0] - 780.0),
    )
    east_corridors = sorted(
        [
            node
            for node in nodes.values()
            if node.city in {"east", "corridor"} and node.kind in {"pop", "colo", "mixed"}
        ],
        key=lambda node: abs(node.point[0] - 880.0),
    )
    for index in range(int(profile_spec["links"]["cross_city_budget"])):
        left = west_corridors[index % len(west_corridors)]
        right = east_corridors[index % len(east_corridors)]
        add_link(links, nodes, left.id, right.id, "backbone")

    adjacency = {node_id: set() for node_id in nodes}
    for link in links.values():
        adjacency[link.a].add(link.b)
        adjacency[link.b].add(link.a)

    west_businesses = [
        node for node in nodes.values() if node.city == "west" and node.kind == "business"
    ]
    origin = min(west_businesses, key=lambda node: dist(node.point, city_points["west"])).id
    local_candidates = sorted(
        [
            node
            for node in nodes.values()
            if node.city in {"west", "corridor"} and node.id != origin
        ],
        key=lambda node: (
            {"dc": 0, "business": 1, "mixed": 2, "pop": 3, "colo": 4, "residence": 5}[node.kind],
            dist(nodes[origin].point, node.point),
        ),
    )
    query_targets = [dc_ids["west"]]
    for node in local_candidates:
        if node.id in query_targets:
            continue
        query_targets.append(node.id)
        if len(query_targets) >= int(profile_spec["activity"]["query_targets"]):
            break

    remote_candidates = sorted(
        [node for node in nodes.values() if node.city == "east" and node.kind != "residence"],
        key=lambda node: (
            {"dc": 0, "pop": 1, "colo": 2, "business": 3, "mixed": 4}[node.kind],
            dist(nodes[dc_ids["east"]].point, node.point),
        ),
    )
    remote_targets = [dc_ids["east"]]
    for node in remote_candidates:
        if node.id in remote_targets:
            continue
        remote_targets.append(node.id)
        if len(remote_targets) >= int(profile_spec["activity"]["remote_targets"]):
            break

    warm_targets = [origin]
    for node_id in query_targets:
        if node_id not in warm_targets:
            warm_targets.append(node_id)
        if len(warm_targets) >= int(profile_spec["activity"]["warm_targets"]):
            break

    inference_targets = list(
        dict.fromkeys(warm_targets + query_targets[: int(len(query_targets) * 0.7)])
    )
    for node in sorted(
        [node for node in nodes.values() if node.city in {"west", "corridor"}],
        key=lambda node: (
            {"business": 0, "mixed": 1, "pop": 2, "colo": 3, "residence": 4, "dc": 5}[node.kind],
            dist(nodes[dc_ids["west"]].point, node.point),
        ),
    ):
        if node.id not in inference_targets:
            inference_targets.append(node.id)
        if len(inference_targets) >= int(profile_spec["activity"]["inference_targets"]):
            break
    if remote_targets:
        inference_targets.append(remote_targets[min(1, len(remote_targets) - 1)])

    query_paths = {node_id: shortest_path(adjacency, origin, node_id) for node_id in query_targets}
    inference_paths = {
        node_id: shortest_path(adjacency, dc_ids["west"], node_id) for node_id in inference_targets
    }
    summary_path = shortest_path(adjacency, dc_ids["west"], dc_ids["east"])

    warm_cycle = [node_id for node_id in warm_targets if node_id != origin] or [dc_ids["west"]]
    remote_cycle = [node_id for node_id in remote_targets if node_id != dc_ids["east"]] or [
        dc_ids["east"]
    ]
    chunk_groups = []
    for index in range(int(profile_spec["activity"]["chunk_groups"])):
        source = remote_cycle[index % len(remote_cycle)]
        target = warm_cycle[index % len(warm_cycle)]
        chunk_groups.append(
            {
                "source": source,
                "target": target,
                "remote_path": shortest_path(adjacency, source, dc_ids["east"]),
                "backbone_path": summary_path,
                "local_path": shortest_path(adjacency, dc_ids["west"], target),
            }
        )

    activities = ActivityPlan(
        origin=origin,
        query_targets=query_targets,
        remote_targets=remote_targets,
        warm_targets=warm_targets,
        inference_targets=inference_targets,
        query_paths=query_paths,
        inference_paths=inference_paths,
        chunk_groups=chunk_groups,
        summary_path=summary_path,
    )

    node_order = {
        node_id: index
        for index, node_id in enumerate(sorted(nodes, key=lambda key: nodes[key].reveal_rank))
    }
    edge_order = {
        node_key(link.a, link.b): index
        for index, link in enumerate(sorted(links.values(), key=lambda item: item.reveal_rank))
    }
    timeline = []
    elapsed = 0.0
    for duration in BEAT_DURATIONS:
        timeline.append((elapsed, elapsed + duration))
        elapsed += duration

    return SceneGraph(
        profile=profile_name,
        scene_spec=scene_spec,
        profile_spec=profile_spec,
        nodes=nodes,
        links=sorted(links.values(), key=lambda item: item.reveal_rank),
        adjacency=adjacency,
        activities=activities,
        edge_order=edge_order,
        node_order=node_order,
        state_outline=outline,
        city_points=city_points,
        dc_ids=dc_ids,
        phases=[(beat["label"], beat["summary"]) for beat in scene_spec["titles"]["beats"]],
        timeline=timeline,
        width=width,
        height=height,
        fps=int(scene_spec["canvas"]["fps"]),
        duration_seconds=int(scene_spec["canvas"]["duration_seconds"]),
        gif_width=int(scene_spec["canvas"]["gif_width"]),
    )


def progress_for(scene: SceneGraph, second: float) -> list[float]:
    return [clamp((second - start) / max(end - start, 1e-6)) for start, end in scene.timeline]


def phase_index(second: float, scene: SceneGraph) -> int:
    for index, (start, end) in enumerate(scene.timeline):
        if start <= second < end:
            return index
    return len(scene.timeline) - 1


def point_along_polyline(points: list[tuple[float, float]], fraction: float) -> tuple[float, float]:
    if len(points) == 1:
        return points[0]
    lengths = []
    total = 0.0
    for left, right in zip(points, points[1:], strict=False):
        segment = dist(left, right)
        lengths.append(segment)
        total += segment
    if total <= 0.0:
        return points[0]
    target = clamp(fraction) * total
    covered = 0.0
    for segment, left, right in zip(lengths, points, points[1:], strict=False):
        if covered + segment >= target:
            local = (target - covered) / segment if segment else 0.0
            return mix_points(left, right, local)
        covered += segment
    return points[-1]


def draw_polyline(
    draw: ImageDraw.ImageDraw,
    points: list[tuple[float, float]],
    color: tuple[int, int, int, int],
    width: int,
) -> None:
    if len(points) >= 2:
        draw.line(points, fill=color, width=width, joint="curve")


def glow_dot(
    overlay: Image.Image,
    point: tuple[float, float],
    radius: float,
    color: tuple[int, int, int, int],
    blur: int,
) -> None:
    glow = Image.new("RGBA", overlay.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(glow)
    x, y = point
    draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=color)
    blurred = glow.filter(ImageFilter.GaussianBlur(radius=blur))
    overlay.alpha_composite(blurred)


def draw_shape(
    draw: ImageDraw.ImageDraw,
    node: Node,
    fill: tuple[int, int, int, int],
    outline: tuple[int, int, int, int],
) -> None:
    x, y = node.point
    r = node.radius
    if node.kind in {"residence", "business", "mixed", "dc"}:
        draw.ellipse((x - r, y - r, x + r, y + r), fill=fill, outline=outline, width=2)
        if node.kind == "dc":
            inner = r * 0.42
            draw.ellipse((x - inner, y - inner, x + inner, y + inner), fill=outline)
    elif node.kind == "colo":
        draw.rounded_rectangle(
            (x - r, y - r, x + r, y + r), radius=4, fill=fill, outline=outline, width=2
        )
    else:
        points = [(x, y - r), (x + r, y), (x, y + r), (x - r, y)]
        draw.polygon(points, fill=fill, outline=outline)


def render_background(scene: SceneGraph, second: float) -> Image.Image:
    colors = scene.scene_spec["colors"]
    image = Image.new("RGBA", (scene.width, scene.height), rgba(colors["background"]))
    glow = Image.new("RGBA", image.size, (0, 0, 0, 0))
    glow_draw = ImageDraw.Draw(glow)
    west = scene.city_points["west"]
    east = scene.city_points["east"]
    pulse = 0.55 + 0.45 * math.sin(second * 0.8)
    glow_draw.ellipse(
        (west[0] - 270, west[1] - 220, west[0] + 270, west[1] + 220),
        fill=rgba(colors["metro_glow_west"], 0.18 + pulse * 0.05),
    )
    glow_draw.ellipse(
        (east[0] - 260, east[1] - 210, east[0] + 260, east[1] + 210),
        fill=rgba(colors["metro_glow_east"], 0.18 + (1.0 - pulse) * 0.05),
    )
    glow_draw.ellipse((520, 270, 1110, 620), fill=rgba(colors["corridor_glow"], 0.18))
    image.alpha_composite(glow.filter(ImageFilter.GaussianBlur(radius=64)))

    scan = Image.new("RGBA", image.size, (0, 0, 0, 0))
    scan_draw = ImageDraw.Draw(scan)
    for y in range(70, scene.height, 44):
        alpha = 0.03 + 0.02 * ((y // 44) % 2)
        scan_draw.line((0, y, scene.width, y), fill=rgba(colors["state_stroke"], alpha), width=1)
    image.alpha_composite(scan)

    state = Image.new("RGBA", image.size, (0, 0, 0, 0))
    state_draw = ImageDraw.Draw(state)
    state_draw.polygon(
        scene.state_outline,
        fill=rgba(colors["state_fill"], 0.92),
        outline=rgba(colors["state_stroke"], 0.82),
    )
    image.alpha_composite(state)
    return image


def render_topology(scene: SceneGraph, second: float) -> Image.Image:
    colors = scene.scene_spec["colors"]
    image = render_background(scene, second)
    draw = ImageDraw.Draw(image)
    p = progress_for(scene, second)
    reveal = ease(clamp(p[0] * 1.28))

    warm_overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    warm_progress = ease(max(p[2] - 0.12, 0.0)) * 0.95 + ease(max(p[3] - 0.05, 0.0)) * 0.45
    warm_nodes = set(scene.activities.warm_targets + [scene.dc_ids["west"], scene.dc_ids["east"]])
    warm_edges = set()
    for group in scene.activities.chunk_groups:
        for path_name in ("backbone_path", "local_path"):
            path = group[path_name]
            for left, right in zip(path, path[1:], strict=False):
                warm_edges.add(node_key(left, right))
    for node_id in warm_nodes:
        node = scene.nodes[node_id]
        if node.kind == "dc":
            glow_dot(
                warm_overlay,
                node.point,
                node.radius + 12,
                rgba(colors["warm"], warm_progress * 0.36),
                18,
            )
        elif warm_progress > 0:
            glow_dot(
                warm_overlay,
                node.point,
                node.radius + 6,
                rgba(colors["warm"], warm_progress * 0.22),
                12,
            )
    image.alpha_composite(warm_overlay)
    draw = ImageDraw.Draw(image)

    edge_total = max(len(scene.links), 1)
    node_total = max(len(scene.nodes), 1)
    for link in scene.links:
        rank = scene.edge_order[node_key(link.a, link.b)] / edge_total
        visible = 1.0 if reveal >= 0.94 else clamp((reveal - rank * 0.82) / 0.24)
        if visible <= 0:
            continue
        color_name = "link_backbone" if link.kind == "backbone" else "link_idle"
        base_alpha = 0.22 if link.kind == "backbone" else 0.15
        if node_key(link.a, link.b) in warm_edges:
            base_alpha += warm_progress * 0.08
        width = 3 if link.kind == "backbone" else 2
        draw_polyline(
            draw,
            path_points([link.a, link.b], scene.nodes),
            rgba(colors[color_name], base_alpha * visible),
            width,
        )

    active_overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    active_draw = ImageDraw.Draw(active_overlay)

    query_progress = p[1]
    origin = scene.nodes[scene.activities.origin]
    if query_progress > 0:
        ring_progress = (query_progress * 3.4) % 1.0
        ring_radius = lerp(origin.radius + 8, origin.radius + 38, ring_progress)
        active_draw.ellipse(
            (
                origin.point[0] - ring_radius,
                origin.point[1] - ring_radius,
                origin.point[0] + ring_radius,
                origin.point[1] + ring_radius,
            ),
            outline=rgba(colors["query"], (1.0 - ring_progress) * 0.75),
            width=3,
        )
        for index, node_id in enumerate(scene.activities.query_targets):
            start = index / max(len(scene.activities.query_targets), 1) * 0.58
            local = clamp((query_progress - start) / 0.33)
            if local <= 0:
                continue
            path = path_points(scene.activities.query_paths[node_id], scene.nodes)
            glow_alpha = 0.12 + ease(local) * 0.55
            draw_polyline(active_draw, path, rgba(colors["query"], glow_alpha), 4)
            point = point_along_polyline(path, ease(local))
            glow_dot(active_overlay, point, 9.0, rgba(colors["query_glow"], 0.5 * local), 7)

    chunk_progress = p[2]
    if chunk_progress > 0:
        summary_alpha = clamp(chunk_progress / 0.18)
        summary_points = path_points(scene.activities.summary_path, scene.nodes)
        draw_polyline(
            active_draw, summary_points, rgba(colors["query"], 0.10 + summary_alpha * 0.22), 3
        )
        for index, group in enumerate(scene.activities.chunk_groups):
            start = 0.08 + index / max(len(scene.activities.chunk_groups), 1) * 0.56
            local = clamp((chunk_progress - start) / 0.26)
            if local <= 0:
                continue
            for path_name, width in (("remote_path", 3), ("backbone_path", 4), ("local_path", 4)):
                path = path_points(group[path_name], scene.nodes)
                draw_polyline(active_draw, path, rgba(colors["chunk"], 0.08 + 0.42 * local), width)
                point = point_along_polyline(path, ease(local))
                glow_dot(
                    active_overlay,
                    point,
                    8.0 if path_name != "backbone_path" else 10.0,
                    rgba(colors["chunk_glow"], 0.65 * local),
                    8,
                )

    inference_progress = p[3]
    if inference_progress > 0:
        for index, node_id in enumerate(scene.activities.inference_targets):
            start = index / max(len(scene.activities.inference_targets), 1) * 0.55
            local = clamp((inference_progress - start) / 0.28)
            if local <= 0:
                continue
            path = path_points(scene.activities.inference_paths[node_id], scene.nodes)
            draw_polyline(active_draw, path, rgba(colors["inference"], 0.07 + 0.34 * local), 3)
            offset = (ease(local) + (index % 3) * 0.18) % 1.0
            point = point_along_polyline(path, offset)
            glow_dot(active_overlay, point, 7.0, rgba(colors["inference_glow"], 0.58 * local), 7)

    image.alpha_composite(active_overlay.filter(ImageFilter.GaussianBlur(radius=3)))
    image.alpha_composite(active_overlay)
    draw = ImageDraw.Draw(image)

    for node_id, node in sorted(scene.nodes.items(), key=lambda item: item[1].reveal_rank):
        rank = scene.node_order[node_id] / node_total
        visible = 1.0 if reveal >= 0.94 else clamp((reveal - rank * 0.82) / 0.22)
        if visible <= 0:
            continue
        glow_alpha = 0.0
        if node_id == scene.activities.origin:
            glow_alpha = max(glow_alpha, 0.34 + query_progress * 0.16)
        if node_id in warm_nodes:
            glow_alpha = max(glow_alpha, 0.16 + warm_progress * 0.20)
        if node_id in scene.activities.inference_targets and inference_progress > 0:
            glow_alpha = max(glow_alpha, 0.12 + inference_progress * 0.18)
        if glow_alpha > 0:
            overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
            tone = colors["warm"] if node_id in warm_nodes else colors["query"]
            glow_dot(overlay, node.point, node.radius + 7, rgba(tone, glow_alpha * visible), 10)
            image.alpha_composite(overlay)
            draw = ImageDraw.Draw(image)

        fill_map = {
            "dc": colors["dc_fill"],
            "residence": colors["residence_fill"],
            "business": colors["business_fill"],
            "colo": colors["colo_fill"],
            "pop": colors["pop_fill"],
            "mixed": colors["mixed_fill"],
        }
        outline_color = (
            colors["query"] if node_id == scene.activities.origin else colors["text_primary"]
        )
        fill = rgba(fill_map[node.kind], 0.72 + 0.28 * visible)
        outline = rgba(outline_color, 0.74 + 0.26 * visible)
        draw_shape(draw, node, fill, outline)

    draw_labels(draw, scene, second)
    return image


def draw_labels(draw: ImageDraw.ImageDraw, scene: SceneGraph, second: float) -> None:
    colors = scene.scene_spec["colors"]
    index = phase_index(second, scene)
    title_font = font(32, bold=True)
    subtitle_font = font(18, bold=True)
    body_font = font(18)
    small_font = font(15)

    draw.text(
        (72, 56),
        scene.scene_spec["titles"]["main"],
        font=title_font,
        fill=rgba(colors["text_primary"]),
    )
    draw.text(
        (74, 102),
        f"Density: {scene.profile.title()} mesh",
        font=subtitle_font,
        fill=rgba(colors["query"]),
    )
    beat_label, beat_summary = scene.phases[index]
    draw.text((74, 132), beat_label, font=subtitle_font, fill=rgba(colors["text_primary"]))
    draw.text((74, 158), beat_summary, font=body_font, fill=rgba(colors["text_secondary"]))

    def draw_tag(
        text: str,
        origin: tuple[float, float],
        text_font: ImageFont.ImageFont,
        fill: tuple[int, int, int, int],
    ) -> None:
        bbox = draw.textbbox((0, 0), text, font=text_font)
        width = bbox[2] - bbox[0]
        height = bbox[3] - bbox[1]
        left, top = origin
        draw.rounded_rectangle(
            (left - 10, top - 6, left + width + 12, top + height + 8),
            radius=6,
            fill=rgba(colors["background"], 0.62),
            outline=rgba(colors["state_stroke"], 0.35),
            width=1,
        )
        draw.text(origin, text, font=text_font, fill=fill)

    city_font = font(20, bold=True)
    city_positions = {
        "west": (scene.city_points["west"][0] - 56, scene.city_points["west"][1] - 104),
        "east": (scene.city_points["east"][0] + 18, scene.city_points["east"][1] - 92),
    }
    for key, data in scene.scene_spec["layout"]["cities"].items():
        point = tuple(data["point"])
        _ = point
        draw_tag(data["label"], city_positions[key], city_font, rgba(colors["text_primary"], 0.95))
    for dc_id in scene.dc_ids.values():
        node = scene.nodes[dc_id]
        label_origin = (node.point[0] + 26, node.point[1] - 22)
        if dc_id == scene.dc_ids["east"]:
            label_origin = (node.point[0] + 30, node.point[1] - 18)
        draw_tag(node.label or dc_id, label_origin, body_font, rgba(colors["text_secondary"]))

    legend_x = 74
    legend_y = scene.height - 164
    draw.text(
        (legend_x, legend_y - 30),
        "Conceptual view",
        font=subtitle_font,
        fill=rgba(colors["text_primary"]),
    )
    for offset, line in enumerate(scene.scene_spec["legend"]):
        draw.text(
            (legend_x, legend_y + offset * 22),
            f"- {line}",
            font=small_font,
            fill=rgba(colors["text_secondary"]),
        )

    phase_x = scene.width - 612
    phase_y = scene.height - 70
    box_w = 130
    for idx, label in enumerate(scene.scene_spec["phase_labels"]):
        active = idx == index
        left = phase_x + idx * (box_w + 12)
        fill = rgba(colors["phase_active"], 0.78) if active else rgba(colors["phase_idle"], 0.88)
        outline = rgba(colors["query"], 0.86) if active else rgba(colors["state_stroke"], 0.72)
        draw.rounded_rectangle(
            (left, phase_y, left + box_w, phase_y + 28),
            radius=4,
            fill=fill,
            outline=outline,
            width=2,
        )
        text_fill = rgba(colors["background"]) if active else rgba(colors["text_primary"])
        bbox = draw.textbbox((0, 0), label, font=small_font)
        tw = bbox[2] - bbox[0]
        draw.text((left + (box_w - tw) / 2, phase_y + 6), label, font=small_font, fill=text_fill)


def summarize_scene(scene: SceneGraph) -> dict:
    return {
        "profile": scene.profile,
        "node_count": len(scene.nodes),
        "link_count": len(scene.links),
        "query_target_count": len(scene.activities.query_targets),
        "remote_target_count": len(scene.activities.remote_targets),
        "warm_target_count": len(scene.activities.warm_targets),
        "inference_target_count": len(scene.activities.inference_targets),
        "chunk_group_count": len(scene.activities.chunk_groups),
        "origin": scene.activities.origin,
        "dc_ids": scene.dc_ids,
        "counts_by_kind": {
            kind: len([node for node in scene.nodes.values() if node.kind == kind])
            for kind in ("dc", "residence", "business", "colo", "pop", "mixed")
        },
    }


def ensure_command(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise SystemExit(f"required command not found: {name}")
    return path


def _require_pillow() -> None:
    if Image is None or ImageDraw is None or ImageFilter is None or ImageFont is None:
        raise SystemExit("Pillow is required for render; install it with the repo dev extras.")


def render_profile(
    scene: SceneGraph,
    formats: set[str],
    output_dir: Path,
    frame_limit: int | None = None,
) -> None:
    _require_pillow()
    output_dir.mkdir(parents=True, exist_ok=True)
    total_frames = scene.duration_seconds * scene.fps
    if frame_limit is not None:
        total_frames = min(total_frames, frame_limit)

    profile_slug = f"zone_mesh_density_{scene.profile}"
    with tempfile.TemporaryDirectory(prefix=f"{profile_slug}_") as tmp_dir_raw:
        tmp_dir = Path(tmp_dir_raw)
        for frame_index in range(total_frames):
            second = frame_index / scene.fps
            frame = render_topology(scene, second)
            frame.save(tmp_dir / f"frame_{frame_index:04d}.png")

        if "mp4" in formats or "all" in formats:
            ffmpeg = ensure_command("ffmpeg")
            subprocess.run(  # noqa: S603
                [
                    ffmpeg,
                    "-y",
                    "-framerate",
                    str(scene.fps),
                    "-i",
                    str(tmp_dir / "frame_%04d.png"),
                    "-c:v",
                    "libx264",
                    "-pix_fmt",
                    "yuv420p",
                    "-movflags",
                    "+faststart",
                    str(output_dir / f"{profile_slug}.mp4"),
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        if "webm" in formats or "all" in formats:
            ffmpeg = ensure_command("ffmpeg")
            subprocess.run(  # noqa: S603
                [
                    ffmpeg,
                    "-y",
                    "-framerate",
                    str(scene.fps),
                    "-i",
                    str(tmp_dir / "frame_%04d.png"),
                    "-c:v",
                    "libvpx-vp9",
                    "-pix_fmt",
                    "yuva420p",
                    "-b:v",
                    "0",
                    "-crf",
                    "32",
                    str(output_dir / f"{profile_slug}.webm"),
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        if "gif" in formats or "all" in formats:
            ffmpeg = ensure_command("ffmpeg")
            palette = tmp_dir / "palette.png"
            subprocess.run(  # noqa: S603
                [
                    ffmpeg,
                    "-y",
                    "-framerate",
                    str(scene.fps),
                    "-i",
                    str(tmp_dir / "frame_%04d.png"),
                    "-vf",
                    f"fps={scene.fps},scale={scene.gif_width}:-1:flags=lanczos,palettegen=max_colors=256",
                    str(palette),
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            subprocess.run(  # noqa: S603
                [
                    ffmpeg,
                    "-y",
                    "-framerate",
                    str(scene.fps),
                    "-i",
                    str(tmp_dir / "frame_%04d.png"),
                    "-i",
                    str(palette),
                    "-lavfi",
                    f"fps={scene.fps},scale={scene.gif_width}:-1:flags=lanczos[x];[x][1:v]paletteuse=dither=sierra2_4a",
                    str(output_dir / f"{profile_slug}.gif"),
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render regional cognitive mesh animations.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect_parser = subparsers.add_parser("inspect", help="Print topology summary as JSON.")
    inspect_parser.add_argument("--profile", choices=["low", "medium", "high"], required=True)

    render_parser = subparsers.add_parser("render", help="Render one or more profiles.")
    render_parser.add_argument("--profile", choices=["low", "medium", "high", "all"], default="all")
    render_parser.add_argument("--format", choices=["mp4", "webm", "gif", "all"], default="all")
    render_parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    render_parser.add_argument(
        "--frame-limit",
        type=int,
        default=None,
        help="Debug option to render only the first N frames.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    scene_spec, profiles = load_specs()
    if args.command == "inspect":
        scene = build_scene(scene_spec, args.profile, profiles[args.profile])
        json.dump(summarize_scene(scene), sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0

    profile_names = ["low", "medium", "high"] if args.profile == "all" else [args.profile]

    formats = {args.format}
    for profile_name in profile_names:
        scene = build_scene(scene_spec, profile_name, profiles[profile_name])
        render_profile(
            scene=scene,
            formats=formats,
            output_dir=args.output_dir,
            frame_limit=args.frame_limit,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
