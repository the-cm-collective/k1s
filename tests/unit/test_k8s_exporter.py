from pathlib import Path

from ae.controller.spec import load_manifest, PortSpec, ServiceSpec
from ae.k8s.exporter import ExportOptions, export_k8s_docs, export_k8s_yaml
from ae.k8s.check import k8s_portability_issues


def test_export_k8s_minimal_echo(tmp_path: Path) -> None:
    man = load_manifest(Path("specs/examples/echo.yaml"))
    opts = ExportOptions(namespace="demo", ingress_class_name="traefik", service_port=80)
    docs = export_k8s_docs(man, options=opts)

    # Expect Deployment, Service, Ingress
    kinds = [d["kind"] for d in docs]
    assert kinds == ["Deployment", "Service", "Ingress"]

    dep = docs[0]
    assert dep["apiVersion"] == "apps/v1"
    assert dep["metadata"]["name"] == man.metadata.name
    assert dep["metadata"]["namespace"] == "demo"
    c = dep["spec"]["template"]["spec"]["containers"][0]
    assert c["image"] == man.spec.image
    assert any(p.get("containerPort") == 8080 for p in c.get("ports", []))
    # env includes valueFrom references for config/secret mappings
    env = {e["name"]: e for e in c.get("env", [])}
    assert env.get("APP_MODE")["valueFrom"]["configMapKeyRef"]["name"] == "app-config"
    assert env.get("APP_MODE")["valueFrom"]["configMapKeyRef"]["key"] == "mode"
    assert env.get("API_TOKEN")["valueFrom"]["secretKeyRef"]["name"] == "demo-secret"
    assert env.get("API_TOKEN")["valueFrom"]["secretKeyRef"]["key"] == "token"

    svc = docs[1]
    assert svc["kind"] == "Service"
    port = svc["spec"]["ports"][0]
    assert port["port"] == 80 and port["targetPort"] == 8080

    ing = docs[2]
    assert ing["spec"]["ingressClassName"] == "traefik"
    rule = ing["spec"]["rules"][0]
    assert rule["host"] == man.spec.ingress.host

    # YAML render sanity
    out = export_k8s_yaml(man, options=opts)
    assert "kind: Deployment" in out and "kind: Service" in out and "kind: Ingress" in out


def test_k8s_check_echo_manifest() -> None:
    man = load_manifest(Path("specs/examples/echo.yaml"))
    issues = k8s_portability_issues(man)
    # Should be only warnings (e.g., multi-arch unknown, maybe security)
    assert all(i.level in {"warn"} for i in issues)


def test_export_with_storage_and_serviceaccount() -> None:
    man = load_manifest(Path("specs/examples/echo.yaml"))
    # Fake storage entry to test PVC emission
    man = man.model_copy(
        update={
            "spec": man.spec.model_copy(
                update={
                    "storage": [
                        {"name": "data", "mountPath": "/data", "retention": "Retain", "size": "2Gi"}
                    ]
                }
            )
        }
    )
    # Bump replicas so PDB is eligible
    man = man.model_copy(update={"spec": man.spec.model_copy(update={"replicas": 2})})
    opts = ExportOptions(
        namespace="demo",
        emit_storage=True,
        service_account_name="web-sa",
        emit_pdb=True,
        pdb_min_available=2,
        hpa_min=1,
        hpa_max=3,
        hpa_cpu_target=70,
        hpa_mem_target=75,
        default_security=True,
    )
    docs = export_k8s_docs(man, options=opts)
    kinds = [d["kind"] for d in docs]
    assert "PersistentVolumeClaim" in kinds
    assert "PodDisruptionBudget" in kinds
    assert "HorizontalPodAutoscaler" in kinds
    dep = next(d for d in docs if d["kind"] == "Deployment")
    pod = dep["spec"]["template"]["spec"]
    assert pod.get("serviceAccountName") == "web-sa"
    vols = pod.get("volumes", [])
    vms = pod["containers"][0].get("volumeMounts", [])
    assert any(v.get("persistentVolumeClaim", {}).get("claimName") == "echo-data" for v in vols)
    assert any(vm.get("mountPath") == "/data" for vm in vms)
    # default security applied when none provided
    sc = pod["containers"][0].get("securityContext", {})
    assert sc.get("readOnlyRootFilesystem") is True
    # PDB has the requested minAvailable
    pdb = next(d for d in docs if d["kind"] == "PodDisruptionBudget")
    assert int(pdb["spec"]["minAvailable"]) == 2
    # HPA has both CPU and memory metrics
    hpa = next(d for d in docs if d["kind"] == "HorizontalPodAutoscaler")
    names = [m["resource"]["name"] for m in hpa["spec"]["metrics"]]
    assert set(names) >= {"cpu", "memory"}


def test_pdb_max_unavailable() -> None:
    man = load_manifest(Path("specs/examples/echo.yaml"))
    man = man.model_copy(update={"spec": man.spec.model_copy(update={"replicas": 2})})
    opts = ExportOptions(namespace="demo", emit_pdb=True, pdb_max_unavailable=1)
    docs = export_k8s_docs(man, options=opts)
    pdb = next(d for d in docs if d["kind"] == "PodDisruptionBudget")
    assert pdb["spec"].get("maxUnavailable") == 1
    assert "minAvailable" not in pdb["spec"]


def test_hpa_memory_average_value() -> None:
    man = load_manifest(Path("specs/examples/echo.yaml"))
    # need HPA bounds
    opts = ExportOptions(
        namespace="demo", hpa_min=1, hpa_max=3, hpa_mem_type="value", hpa_mem_value="200Mi"
    )
    docs = export_k8s_docs(man, options=opts)
    hpa = next(d for d in docs if d["kind"] == "HorizontalPodAutoscaler")
    mem = next(m for m in hpa["spec"]["metrics"] if m["resource"]["name"] == "memory")
    assert mem["resource"]["target"]["type"] == "AverageValue"
    assert mem["resource"]["target"]["averageValue"] == "200Mi"


def test_service_multi_port_and_ingress_backend() -> None:
    man = load_manifest(Path("specs/examples/echo.yaml"))
    # Add a metrics container port and explicit multi-port Service mapping
    svc = ServiceSpec(
        ports=[
            ServiceSpec.ServicePort(name="http", port=80, targetPort=8080),
            ServiceSpec.ServicePort(name="metrics", port=9090, targetPort=9090),
        ]
    )
    man = man.model_copy(
        update={
            "spec": man.spec.model_copy(
                update={
                    "ports": man.spec.ports + [PortSpec(name="metrics", containerPort=9090)],
                    "service": svc,
                }
            )
        }
    )
    opts = ExportOptions(namespace="demo")
    docs = export_k8s_docs(man, options=opts)
    svc = next(d for d in docs if d["kind"] == "Service")
    ports = svc["spec"]["ports"]
    # Expect both ports present with correct mapping
    names = {p["name"] for p in ports}
    assert names == {"http", "metrics"}
    p_http = next(p for p in ports if p["name"] == "http")
    p_metrics = next(p for p in ports if p["name"] == "metrics")
    assert p_http["port"] == 80 and p_http["targetPort"] == 8080
    assert p_metrics["port"] == 9090 and p_metrics["targetPort"] == 9090
    # Ingress should route to the 'http' service port (80)
    ing = next(d for d in docs if d["kind"] == "Ingress")
    backend = ing["spec"]["rules"][0]["http"]["paths"][0]["backend"]["service"]["port"]["number"]
    assert int(backend) == 80


def test_hpa_guard_blocks_without_requests_cpu() -> None:
    man = load_manifest(Path("specs/examples/echo.yaml"))
    # Remove requests
    man = man.model_copy(update={"spec": man.spec.model_copy(update={"resources": None})})
    opts = ExportOptions(namespace="demo", hpa_min=1, hpa_max=3, hpa_cpu_target=70)
    import pytest

    with pytest.raises(ValueError):
        export_k8s_docs(man, options=opts)


def test_hpa_guard_allows_override_flag() -> None:
    man = load_manifest(Path("specs/examples/echo.yaml"))
    man = man.model_copy(update={"spec": man.spec.model_copy(update={"resources": None})})
    opts = ExportOptions(
        namespace="demo", hpa_min=1, hpa_max=3, hpa_cpu_target=70, allow_hpa_without_requests=True
    )
    docs = export_k8s_docs(man, options=opts)
    assert any(d["kind"] == "HorizontalPodAutoscaler" for d in docs)


def test_service_type_nodeport_with_explicit_nodeports() -> None:
    man = load_manifest(Path("specs/examples/echo.yaml"))
    # Multi-port with NodePort service type and explicit nodePorts
    from ae.controller.spec import ServiceSpec, PortSpec

    man = man.model_copy(
        update={
            "spec": man.spec.model_copy(
                update={
                    "ports": [
                        PortSpec(name="http", containerPort=8080),
                        PortSpec(name="metrics", containerPort=9090),
                    ],
                    "service": ServiceSpec(
                        type="NodePort",
                        ports=[
                            ServiceSpec.ServicePort(
                                name="http", port=80, targetPort=8080, nodePort=30080
                            ),
                            ServiceSpec.ServicePort(
                                name="metrics", port=9090, targetPort=9090, nodePort=32090
                            ),
                        ],
                    ),
                }
            )
        }
    )
    docs = export_k8s_docs(man, options=ExportOptions(namespace="demo"))
    svc = next(d for d in docs if d["kind"] == "Service")
    assert svc["spec"]["type"] == "NodePort"
    ports = svc["spec"]["ports"]
    p_http = next(p for p in ports if p["name"] == "http")
    p_metrics = next(p for p in ports if p["name"] == "metrics")
    assert p_http.get("nodePort") == 30080
    assert p_metrics.get("nodePort") == 32090


def test_service_type_loadbalancer_external_traffic_policy() -> None:
    man = load_manifest(Path("specs/examples/echo.yaml"))
    from ae.controller.spec import ServiceSpec

    man = man.model_copy(
        update={
            "spec": man.spec.model_copy(
                update={
                    "service": ServiceSpec(
                        type="LoadBalancer",
                        externalTrafficPolicy="Local",
                        ports=[ServiceSpec.ServicePort(name="http", port=80, targetPort=8080)],
                    )
                }
            )
        }
    )
    docs = export_k8s_docs(man, options=ExportOptions(namespace="demo"))
    svc = next(d for d in docs if d["kind"] == "Service")
    assert svc["spec"]["type"] == "LoadBalancer"
    assert svc["spec"].get("externalTrafficPolicy") == "Local"
    # nodePort should not be present unless explicitly set
    assert "nodePort" not in svc["spec"]["ports"][0]


def test_scheduling_pass_through_affinity_tolerations_topology() -> None:
    man = load_manifest(Path("specs/examples/echo.yaml"))
    # Inject scheduling fields
    man = man.model_copy(
        update={
            "spec": man.spec.model_copy(
                update={
                    "affinity": {
                        "podAntiAffinity": {
                            "preferredDuringSchedulingIgnoredDuringExecution": [
                                {
                                    "weight": 100,
                                    "podAffinityTerm": {
                                        "topologyKey": "kubernetes.io/hostname",
                                        "labelSelector": {
                                            "matchLabels": {"app": man.metadata.name}
                                        },
                                    },
                                }
                            ]
                        }
                    },
                    "tolerations": [
                        {
                            "key": "workload",
                            "operator": "Equal",
                            "value": "apps",
                            "effect": "NoSchedule",
                        }
                    ],
                    "topology_spread_constraints": [
                        {
                            "maxSkew": 1,
                            "topologyKey": "topology.kubernetes.io/zone",
                            "whenUnsatisfiable": "ScheduleAnyway",
                            "labelSelector": {"matchLabels": {"app": man.metadata.name}},
                        }
                    ],
                }
            )
        }
    )
    docs = export_k8s_docs(man, options=ExportOptions(namespace="demo"))
    dep = next(d for d in docs if d["kind"] == "Deployment")
    pod = dep["spec"]["template"]["spec"]
    assert pod.get("affinity") and pod.get("tolerations") and pod.get("topologySpreadConstraints")


def test_security_seccomp_and_apparmor_annotations() -> None:
    man = load_manifest(Path("specs/examples/echo.yaml"))
    # Add security with seccomp Localhost and AppArmor profile
    from ae.controller.spec import SecuritySpec

    sec = SecuritySpec(
        runAsUser=1000,
        readOnlyRootFilesystem=True,
        dropCapabilities=["NET_RAW"],
        seccompProfileType="Localhost",
        seccompLocalhostProfile="profiles/echo.json",
        apparmorProfile="localhost/echo-profile",
    )
    man = man.model_copy(update={"spec": man.spec.model_copy(update={"security": sec})})
    docs = export_k8s_docs(man, options=ExportOptions(namespace="demo"))
    dep = next(d for d in docs if d["kind"] == "Deployment")
    tmpl = dep["spec"]["template"]
    ann = (tmpl.get("metadata") or {}).get("annotations", {})
    key = f"container.apparmor.security.beta.kubernetes.io/{man.metadata.name}"
    assert ann.get(key) == "localhost/echo-profile"
    c_sc = tmpl["spec"]["containers"][0]["securityContext"]
    assert c_sc.get("seccompProfile", {}).get("type") == "Localhost"
    assert c_sc.get("seccompProfile", {}).get("localhostProfile") == "profiles/echo.json"


def test_service_external_ips_and_priority_class() -> None:
    man = load_manifest(Path("specs/examples/echo.yaml"))
    from ae.controller.spec import ServiceSpec

    man = man.model_copy(
        update={
            "spec": man.spec.model_copy(
                update={
                    "service": ServiceSpec(
                        type="ClusterIP",
                        ports=[ServiceSpec.ServicePort(name="http", port=80, targetPort=8080)],
                        externalIPs=["1.2.3.4", "5.6.7.8"],
                    ),
                    "priority_class_name": "high-priority",
                }
            )
        }
    )
    docs = export_k8s_docs(man, options=ExportOptions(namespace="demo"))
    svc = next(d for d in docs if d["kind"] == "Service")
    assert svc["spec"].get("externalIPs") == ["1.2.3.4", "5.6.7.8"]
    dep = next(d for d in docs if d["kind"] == "Deployment")
    assert dep["spec"]["template"]["spec"].get("priorityClassName") == "high-priority"


def test_nodeport_range_validation_raises() -> None:
    man = load_manifest(Path("specs/examples/echo.yaml"))
    from ae.controller.spec import ServiceSpec

    # NodePort out of range
    man = man.model_copy(
        update={
            "spec": man.spec.model_copy(
                update={
                    "service": ServiceSpec(
                        type="NodePort",
                        ports=[
                            ServiceSpec.ServicePort(
                                name="http", port=80, targetPort=8080, nodePort=20000
                            )
                        ],
                    )
                }
            )
        }
    )
    import pytest

    with pytest.raises(ValueError):
        export_k8s_docs(man, options=ExportOptions(namespace="demo"))


def test_duplicate_service_names_and_nodeports_raise() -> None:
    man = load_manifest(Path("specs/examples/echo.yaml"))
    from ae.controller.spec import ServiceSpec

    # Duplicate name
    man1 = man.model_copy(
        update={
            "spec": man.spec.model_copy(
                update={
                    "service": ServiceSpec(
                        type="ClusterIP",
                        ports=[
                            ServiceSpec.ServicePort(name="http", port=80, targetPort=8080),
                            ServiceSpec.ServicePort(name="http", port=81, targetPort=8080),
                        ],
                    )
                }
            )
        }
    )
    import pytest

    with pytest.raises(ValueError):
        export_k8s_docs(man1, options=ExportOptions(namespace="demo"))
    # Duplicate nodePort
    man2 = man.model_copy(
        update={
            "spec": man.spec.model_copy(
                update={
                    "service": ServiceSpec(
                        type="NodePort",
                        ports=[
                            ServiceSpec.ServicePort(
                                name="http", port=80, targetPort=8080, nodePort=30080
                            ),
                            ServiceSpec.ServicePort(
                                name="metrics", port=9090, targetPort=9090, nodePort=30080
                            ),
                        ],
                    )
                }
            )
        }
    )
    with pytest.raises(ValueError):
        export_k8s_docs(man2, options=ExportOptions(namespace="demo"))


def test_network_policy_emit_from_manifest() -> None:
    man = load_manifest(Path("specs/examples/echo.yaml"))
    # Add a simple default-deny egress/ingress policy with podSelector on app label
    man = man.model_copy(
        update={
            "spec": man.spec.model_copy(
                update={
                    "network_policy": {
                        "policyTypes": ["Ingress", "Egress"],
                        "ingress": [],
                        "egress": [],
                    }
                }
            )
        }
    )
    docs = export_k8s_docs(man, options=ExportOptions(namespace="demo"))
    np = next(d for d in docs if d.get("kind") == "NetworkPolicy")
    assert np["metadata"]["name"] == man.metadata.name
    assert np["spec"]["podSelector"]["matchLabels"]["app"] == man.metadata.name
    assert np["spec"]["policyTypes"] == ["Ingress", "Egress"]
