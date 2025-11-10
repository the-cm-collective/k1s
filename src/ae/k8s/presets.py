"""Presets for export-k8s to speed up common profiles."""

from __future__ import annotations

from dataclasses import replace
from typing import Literal

from ae.k8s.exporter import ExportOptions


PresetName = Literal["web-basic", "web-hardened", "scale-ready", "web-strict"]


def apply_preset(opts: ExportOptions, preset: PresetName) -> ExportOptions:
    """Merge a preset into options, without clobbering explicitly set values.

    Fields that are falsy/None in opts will be filled from the preset.
    """

    base = opts

    def fill_bool(value: bool, current: bool) -> bool:
        return current or value

    if preset == "web-basic":
        return replace(
            base,
            default_security=fill_bool(True, base.default_security),
            service_port=base.service_port or 80,
        )

    if preset == "web-hardened":
        return replace(
            base,
            default_security=fill_bool(True, base.default_security),
            emit_pdb=fill_bool(True, base.emit_pdb),
            hpa_min=base.hpa_min or 2,
            hpa_max=base.hpa_max or 4,
            hpa_cpu_target=base.hpa_cpu_target or 70,
            emit_configs=fill_bool(True, base.emit_configs),
            service_account_name=base.service_account_name or "app-sa",
            service_port=base.service_port or 80,
        )

    if preset == "scale-ready":
        return replace(
            base,
            emit_pdb=fill_bool(True, base.emit_pdb),
            hpa_min=base.hpa_min or 2,
            hpa_max=base.hpa_max or 10,
            hpa_cpu_target=base.hpa_cpu_target or 70,
            service_port=base.service_port or 80,
        )

    if preset == "web-strict":
        return replace(
            base,
            require_requests=True if base.require_requests is False else base.require_requests or True,
            default_security=fill_bool(True, base.default_security),
            emit_pdb=fill_bool(True, base.emit_pdb),
            service_port=base.service_port or 80,
        )

    return base
