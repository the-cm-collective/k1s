from pathlib import Path

from ae.controller.spec import load_manifest
from ae.k8s.exporter import ExportOptions, export_k8s_docs


def test_export_multi_container_and_initcontainers() -> None:
    man = load_manifest(Path("specs/examples/echo.yaml"))
    c1 = {"name": "web", "image": man.spec.image, "ports": man.spec.ports, "env": man.spec.env}
    # Sidecar includes per-container health and lifecycle to verify parity
    from ae.controller.spec import ProbeSpec, TCPSocketProbe, HealthSpec, LifecycleSpec, LifecycleHandler
    from ae.controller.spec import AppSpec as _AppSpec
    c2 = _AppSpec.ContainerSpec(
        name="sidecar",
        image=man.spec.image,
        args=["--sidecar"],
        health=HealthSpec(
            readiness=ProbeSpec(tcpSocket=TCPSocketProbe(port=8080), initialDelaySeconds=1, timeoutSeconds=1, periodSeconds=5, successThreshold=1, failureThreshold=3),
            liveness=ProbeSpec(tcpSocket=TCPSocketProbe(port=8080), initialDelaySeconds=3, timeoutSeconds=1, periodSeconds=10, successThreshold=1, failureThreshold=3),
        ),
        lifecycle=LifecycleSpec(postStart=LifecycleHandler(exec={"command": ["/bin/sh", "-c", "echo sidecar"]})),
    )
    init = {"name": "init", "image": man.spec.image, "command": ["/bin/sh", "-c", "echo init"]}
    man = man.model_copy(
        update={"spec": man.spec.model_copy(update={"containers": [c1, c2], "init_containers": [init]})}
    )
    docs = export_k8s_docs(man, options=ExportOptions(namespace="demo"))
    dep = next(d for d in docs if d.get("kind") == "Deployment")
    pod = dep["spec"]["template"]["spec"]
    names = [c["name"] for c in pod.get("containers", [])]
    assert names == ["web", "sidecar"]
    inits = [c["name"] for c in pod.get("initContainers", [])]
    assert inits == ["init"]
    side = next(c for c in pod.get("containers", []) if c["name"] == "sidecar")
    assert side.get("readinessProbe") and side.get("livenessProbe")


def test_projection_mounts_per_init_container() -> None:
    man = load_manifest(Path("specs/examples/echo.yaml"))
    # add a container with projectionMounts mapping a file from projected volume
    from ae.controller.spec import AppSpec as _AppSpec
    init = _AppSpec.ContainerSpec(
        name="init",
        image=man.spec.image,
        projectionMounts=[
            {"path": "config/app_mode.txt", "mountPath": "/etc/app/mode"},
            {"path": "secret/token", "mountPath": "/var/run/token", "readOnly": True},
        ],
    )
    man = man.model_copy(update={"spec": man.spec.model_copy(update={"init_containers": [init]})})
    docs = export_k8s_docs(man, options=ExportOptions(namespace="demo", emit_configs=True, emit_secrets=True))
    dep = next(d for d in docs if d.get("kind") == "Deployment")
    pod = dep["spec"]["template"]["spec"]
    vname = next(v["name"] for v in pod.get("volumes", []) if "projected" in v)
    mounts = pod.get("initContainers", [])[0].get("volumeMounts", [])
    # Ensure subPath mounts were created targeting the projected volume
    assert any(m.get("name") == vname and m.get("subPath") == "config/app_mode.txt" for m in mounts)
    assert any(m.get("name") == vname and m.get("subPath") == "secret/token" and m.get("readOnly") for m in mounts)
