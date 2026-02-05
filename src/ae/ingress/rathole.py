"""Rathole config renderer (core-proxy tunnel)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RatholeServerService:
    name: str
    bind_addr: str


@dataclass(frozen=True)
class RatholeServerConfig:
    bind_addr: str
    default_token: str
    services: list[RatholeServerService]


@dataclass(frozen=True)
class RatholeClientService:
    name: str
    local_addr: str


@dataclass(frozen=True)
class RatholeClientConfig:
    remote_addr: str
    default_token: str
    services: list[RatholeClientService]


def render_rathole_server(config: RatholeServerConfig) -> str:
    lines = [
        "[server]",
        f'bind_addr = "{config.bind_addr}"',
        f'default_token = "{config.default_token}"',
        "",
    ]
    if config.services:
        lines.append("[server.services]")
        for svc in config.services:
            lines.append(f'[server.services."{svc.name}"]')
            lines.append(f'bind_addr = "{svc.bind_addr}"')
            lines.append("")
    return "\n".join(lines).strip() + "\n"


def render_rathole_client(config: RatholeClientConfig) -> str:
    lines = [
        "[client]",
        f'remote_addr = "{config.remote_addr}"',
        f'default_token = "{config.default_token}"',
        "",
    ]
    if config.services:
        lines.append("[client.services]")
        for svc in config.services:
            lines.append(f'[client.services."{svc.name}"]')
            lines.append(f'local_addr = "{svc.local_addr}"')
            lines.append("")
    return "\n".join(lines).strip() + "\n"


def write_rathole_server(path: Path, config: RatholeServerConfig) -> str:
    content = render_rathole_server(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return content


def write_rathole_client(path: Path, config: RatholeClientConfig) -> str:
    content = render_rathole_client(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return content


__all__ = [
    "RatholeServerService",
    "RatholeServerConfig",
    "RatholeClientService",
    "RatholeClientConfig",
    "render_rathole_server",
    "render_rathole_client",
    "write_rathole_server",
    "write_rathole_client",
]
