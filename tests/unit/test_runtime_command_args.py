from __future__ import annotations

from ae.runtime.command_args import kubernetes_command_parts


def test_kubernetes_command_parts_maps_command_to_entrypoint() -> None:
    assert kubernetes_command_parts(
        ["/bin/sh", "-c"],
        ["mc alias set local http://minio:9000"],
    ) == (
        ["/bin/sh"],
        ["-c", "mc alias set local http://minio:9000"],
    )


def test_kubernetes_command_parts_preserves_image_entrypoint_for_args_only() -> None:
    assert kubernetes_command_parts(None, ["server", "/data"]) == (
        None,
        ["server", "/data"],
    )


def test_kubernetes_command_parts_does_not_split_scalar_strings() -> None:
    assert kubernetes_command_parts("/bin/sh", "-c echo ok") == (
        ["/bin/sh"],
        ["-c echo ok"],
    )
