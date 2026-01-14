from pathlib import Path

from ae.controller.spec import (
    HTTPGetProbe,
    PortSpec,
    ProbeSpec,
    ServiceSpec,
    TCPSocketProbe,
    load_manifest,
)
from ae.k8s.check import k8s_portability_issues
from ae.k8s.exporter import ExportOptions, export_k8s_docs, export_k8s_yaml


def test_export_k8s_minimal_echo(tmp_path: Path) -> None:
    _ = tmp_path
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


def test_envfrom_and_projected_volume_exports() -> None:
    man = load_manifest(Path("specs/examples/echo.yaml"))
    # Mark envFrom on both refs and ensure file projections create a projected volume
    man = man.model_copy(
        update={
            "spec": man.spec.model_copy(
                update={
                    "config_refs": [
                        man.spec.config_refs[0].model_copy(update={"env_from": True}),
                    ],
                    "secret_refs": [
                        man.spec.secret_refs[0].model_copy(update={"env_from": True}),
                    ],
                }
            )
        }
    )
    opts = ExportOptions(namespace="demo", emit_configs=True, emit_secrets=True)
    docs = export_k8s_docs(man, options=opts)
    dep = next(d for d in docs if d["kind"] == "Deployment")
    c = dep["spec"]["template"]["spec"]["containers"][0]
    # envFrom should include configMapRef and secretRef
    assert any("configMapRef" in e for e in c.get("envFrom", []))
    assert any("secretRef" in e for e in c.get("envFrom", []))
    # projected volume should be present and mounted at /var/run/ae/config
    pod = dep["spec"]["template"]["spec"]
    vols = pod.get("volumes", [])
    vms = c.get("volumeMounts", [])
    proj = next(v for v in vols if v.get("projected"))
    assert proj["projected"]["sources"]
    assert any(vm.get("mountPath") == "/var/run/ae/config" for vm in vms)


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


def test_pdb_percentage_values_supported() -> None:
    man = load_manifest(Path("specs/examples/echo.yaml"))
    # replicas>1 to make PDB eligible
    man = man.model_copy(update={"spec": man.spec.model_copy(update={"replicas": 4})})
    # Pass a percentage string intentionally (exporter supports coercion fallback)
    opts = ExportOptions(namespace="demo", emit_pdb=True, pdb_min_available="50%")  # type: ignore[arg-type]
    docs = export_k8s_docs(man, options=opts)
    pdb = next(d for d in docs if d["kind"] == "PodDisruptionBudget")
    assert pdb["spec"].get("minAvailable") == "50%"


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


def test_hpa_behavior_knobs_pass_through() -> None:
    man = load_manifest(Path("specs/examples/echo.yaml"))
    opts = ExportOptions(
        namespace="demo",
        hpa_min=1,
        hpa_max=3,
        hpa_cpu_target=70,
        hpa_behavior_up={"stabilizationWindowSeconds": 60, "policies": [{"type": "Percent", "value": 50, "periodSeconds": 60}]},
        hpa_behavior_down={"stabilizationWindowSeconds": 60},
    )
    docs = export_k8s_docs(man, options=opts)
    hpa = next(d for d in docs if d["kind"] == "HorizontalPodAutoscaler")
    behavior = hpa["spec"].get("behavior", {})
    assert behavior.get("scaleUp", {}).get("stabilizationWindowSeconds") == 60
    assert behavior.get("scaleDown", {}).get("stabilizationWindowSeconds") == 60


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
    from ae.controller.spec import PortSpec, ServiceSpec

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


def test_service_session_affinity_clientip_timeout() -> None:
    man = load_manifest(Path("specs/examples/echo.yaml"))
    from ae.controller.spec import ServiceSpec

    man = man.model_copy(
        update={
            "spec": man.spec.model_copy(
                update={
                    "service": ServiceSpec(
                        type="ClusterIP",
                        ports=[ServiceSpec.ServicePort(name="http", port=80, targetPort=8080)],
                        sessionAffinity="ClientIP",
                        sessionAffinityConfig={"clientIP": {"timeoutSeconds": 10800}},
                    )
                }
            )
        }
    )
    docs = export_k8s_docs(man, options=ExportOptions(namespace="demo"))
    svc = next(d for d in docs if d["kind"] == "Service")
    spec = svc["spec"]
    assert spec.get("sessionAffinity") == "ClientIP"
    assert spec.get("sessionAffinityConfig", {}).get("clientIP", {}).get("timeoutSeconds") == 10800


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


def test_exports_startup_probe_and_pull_options() -> None:
    man = load_manifest(Path("specs/examples/echo.yaml"))
    # Inject startup probe and image pull options
    startup = ProbeSpec(
        httpGet=HTTPGetProbe(path="/healthz", port=8080),
        initialDelaySeconds=2,
        timeoutSeconds=1,
    )
    man = man.model_copy(
        update={
            "spec": man.spec.model_copy(
                update={
                    "health": man.spec.health.model_copy(update={"startup": startup}),
                    "image_pull_policy": "IfNotPresent",
                    "image_pull_secrets": ["regcred"],
                }
            )
        }
    )
    docs = export_k8s_docs(man, options=ExportOptions(namespace="demo"))
    dep = next(d for d in docs if d["kind"] == "Deployment")
    c = dep["spec"]["template"]["spec"]["containers"][0]
    assert c.get("startupProbe") and c.get("imagePullPolicy") == "IfNotPresent"
    pod = dep["spec"]["template"]["spec"]
    assert pod.get("imagePullSecrets") == [{"name": "regcred"}]


def test_pod_security_fs_group_and_pod_seccomp() -> None:
    man = load_manifest(Path("specs/examples/echo.yaml"))
    # Add pod-level fsGroup and seccompProfile(Localhost)
    from ae.controller.spec import PodSecuritySpec
    psec = PodSecuritySpec(
        fs_group=2000, seccomp_type="Localhost", seccomp_localhost_profile="profiles/pod.json"
    )
    man = man.model_copy(update={"spec": man.spec.model_copy(update={"pod_security": psec})})
    docs = export_k8s_docs(man, options=ExportOptions(namespace="demo"))
    dep = next(d for d in docs if d["kind"] == "Deployment")
    psec = dep["spec"]["template"]["spec"].get("securityContext", {})
    assert psec.get("fsGroup") == 2000
    assert psec.get("seccompProfile", {}).get("type") == "Localhost"
    assert psec.get("seccompProfile", {}).get("localhostProfile") == "profiles/pod.json"


def test_lifecycle_hooks_exported() -> None:
    man = load_manifest(Path("specs/examples/echo.yaml"))
    # Add lifecycle: postStart exec, preStop httpGet
    man = man.model_copy(
        update={
            "spec": man.spec.model_copy(
                update={
                    "lifecycle": {
                        "postStart": {"exec": {"command": ["/bin/sh", "-c", "echo start"]}},
                        "preStop": {"httpGet": {"path": "/quit", "port": 8080}},
                    }
                }
            )
        }
    )
    docs = export_k8s_docs(man, options=ExportOptions(namespace="demo"))
    dep = next(d for d in docs if d["kind"] == "Deployment")
    c = dep["spec"]["template"]["spec"]["containers"][0]
    lc = c.get("lifecycle", {})
    assert lc.get("postStart", {}).get("exec", {}).get("command")
    assert lc.get("preStop", {}).get("httpGet", {}).get("path") == "/quit"


def test_container_args_and_working_dir_export() -> None:
    man = load_manifest(Path("specs/examples/echo.yaml"))
    man = man.model_copy(
        update={
            "spec": man.spec.model_copy(
                update={
                    "command": ["/bin/app"],
                    "args": ["--flag", "value"],
                    "working_dir": "/work",
                }
            )
        }
    )
    docs = export_k8s_docs(man, options=ExportOptions(namespace="demo"))
    dep = next(d for d in docs if d["kind"] == "Deployment")
    c = dep["spec"]["template"]["spec"]["containers"][0]
    assert c.get("command") == ["/bin/app"]
    assert c.get("args") == ["--flag", "value"]
    assert c.get("workingDir") == "/work"


def test_container_termination_message_fields_export() -> None:
    man = load_manifest(Path("specs/examples/echo.yaml"))
    man = man.model_copy(
        update={
            "spec": man.spec.model_copy(
                update={
                    "termination_message_path": "/var/log/term.msg",
                    "termination_message_policy": "FallbackToLogsOnError",
                }
            )
        }
    )
    docs = export_k8s_docs(man, options=ExportOptions(namespace="demo"))
    dep = next(d for d in docs if d["kind"] == "Deployment")
    c = dep["spec"]["template"]["spec"]["containers"][0]
    assert c.get("terminationMessagePath") == "/var/log/term.msg"
    assert c.get("terminationMessagePolicy") == "FallbackToLogsOnError"


def test_env_valuefrom_fieldref_pass_through() -> None:
    man = load_manifest(Path("specs/examples/echo.yaml"))
    # Add a fieldRef env for metadata.name
    man = man.model_copy(
        update={
            "spec": man.spec.model_copy(
                update={
                    "env": man.spec.env
                    + [
                        {
                            "name": "POD_NAME",
                            "valueFrom": {"fieldRef": {"fieldPath": "metadata.name"}},
                        }
                    ]
                }
            )
        }
    )
    docs = export_k8s_docs(man, options=ExportOptions(namespace="demo"))
    dep = next(d for d in docs if d["kind"] == "Deployment")
    env = dep["spec"]["template"]["spec"]["containers"][0].get("env", [])
    pod_name = next(e for e in env if e.get("name") == "POD_NAME")
    assert pod_name.get("valueFrom", {}).get("fieldRef", {}).get("fieldPath") == "metadata.name"


def test_pod_dns_policy_and_config_exports() -> None:
    man = load_manifest(Path("specs/examples/echo.yaml"))
    man = man.model_copy(
        update={
            "spec": man.spec.model_copy(
                update={
                    "dns_policy": "ClusterFirst",
                    "dns_config": {
                        "nameservers": ["1.1.1.1", "8.8.8.8"],
                        "searches": ["svc.cluster.local", "cluster.local"],
                        "options": [{"name": "ndots", "value": "5"}],
                    },
                }
            )
        }
    )
    docs = export_k8s_docs(man, options=ExportOptions(namespace="demo"))
    dep = next(d for d in docs if d["kind"] == "Deployment")
    pod = dep["spec"]["template"]["spec"]
    assert pod.get("dnsPolicy") == "ClusterFirst"
    assert pod.get("dnsConfig", {}).get("nameservers") == ["1.1.1.1", "8.8.8.8"]
    assert pod.get("dnsConfig", {}).get("searches") == ["svc.cluster.local", "cluster.local"]
    opts = pod.get("dnsConfig", {}).get("options", [])
    assert any(o.get("name") == "ndots" and o.get("value") == "5" for o in opts)


def test_pod_hostname_and_subdomain_export() -> None:
    man = load_manifest(Path("specs/examples/echo.yaml"))
    man = man.model_copy(
        update={
            "spec": man.spec.model_copy(update={"hostname": "echo-0", "subdomain": "echo"})
        }
    )
    docs = export_k8s_docs(man, options=ExportOptions(namespace="demo"))
    dep = next(d for d in docs if d["kind"] == "Deployment")
    pod = dep["spec"]["template"]["spec"]
    assert pod.get("hostname") == "echo-0"
    assert pod.get("subdomain") == "echo"


def test_pod_host_aliases_export() -> None:
    man = load_manifest(Path("specs/examples/echo.yaml"))
    man = man.model_copy(
        update={
            "spec": man.spec.model_copy(
                update={
                    "host_aliases": [
                        {"ip": "10.0.0.10", "hostnames": ["db", "db.local"]},
                        {"ip": "10.0.0.11", "hostnames": ["cache"]},
                    ]
                }
            )
        }
    )
    docs = export_k8s_docs(man, options=ExportOptions(namespace="demo"))
    dep = next(d for d in docs if d["kind"] == "Deployment")
    pod = dep["spec"]["template"]["spec"]
    al = pod.get("hostAliases", [])
    assert {a["ip"] for a in al} == {"10.0.0.10", "10.0.0.11"}
    assert any("db" in a.get("hostnames", []) for a in al)


def test_pod_small_pass_throughs() -> None:
    man = load_manifest(Path("specs/examples/echo.yaml"))
    man = man.model_copy(
        update={
            "spec": man.spec.model_copy(
                update={
                    "enable_service_links": False,
                    "share_process_namespace": True,
                    "host_network": True,
                    "node_selector": {"disktype": "ssd", "zone": "us-east-1a"},
                }
            )
        }
    )
    docs = export_k8s_docs(man, options=ExportOptions(namespace="demo"))
    dep = next(d for d in docs if d["kind"] == "Deployment")
    pod = dep["spec"]["template"]["spec"]
    assert pod.get("enableServiceLinks") is False
    assert pod.get("shareProcessNamespace") is True
    assert pod.get("hostNetwork") is True
    assert pod.get("nodeSelector") == {"disktype": "ssd", "zone": "us-east-1a"}
    # setHostnameAsFQDN
    man2 = man.model_copy(update={"spec": man.spec.model_copy(update={"set_hostname_as_fqdn": True})})
    docs2 = export_k8s_docs(man2, options=ExportOptions(namespace="demo"))
    dep2 = next(d for d in docs2 if d["kind"] == "Deployment")
    pod2 = dep2["spec"]["template"]["spec"]
    assert pod2.get("setHostnameAsFQDN") is True
    # hostPID/hostIPC
    man3 = man.model_copy(update={"spec": man.spec.model_copy(update={"host_pid": True, "host_ipc": False})})
    docs3 = export_k8s_docs(man3, options=ExportOptions(namespace="demo"))
    dep3 = next(d for d in docs3 if d["kind"] == "Deployment")
    pod3 = dep3["spec"]["template"]["spec"]
    assert pod3.get("hostPID") is True and pod3.get("hostIPC") is False


def test_statefulset_carries_startup_probe_and_pull_secrets() -> None:
    man = load_manifest(Path("specs/examples/echo.yaml"))
    startup2 = ProbeSpec(tcpSocket=TCPSocketProbe(port=8080), initialDelaySeconds=1, timeoutSeconds=1)
    man = man.model_copy(
        update={
            "spec": man.spec.model_copy(
                update={
                    "replicas": 2,
                    "health": man.spec.health.model_copy(update={"startup": startup2}),
                    "image_pull_policy": "Always",
                    "image_pull_secrets": ["private-reg"],
                }
            )
        }
    )
    opts = ExportOptions(namespace="demo", workload_kind="StatefulSet", emit_storage=False)
    docs = export_k8s_docs(man, options=opts)
    sts = next(d for d in docs if d["kind"] == "StatefulSet")
    c = sts["spec"]["template"]["spec"]["containers"][0]
    assert c.get("startupProbe") and c.get("imagePullPolicy") == "Always"
    pod = sts["spec"]["template"]["spec"]
    assert pod.get("imagePullSecrets") == [{"name": "private-reg"}]


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


def test_export_default_network_policy_generation() -> None:
    man = load_manifest(Path("specs/examples/echo.yaml"))
    opts = ExportOptions(namespace="demo", emit_network_policy=True, np_default_deny_ingress=True, np_default_deny_egress=True, np_allow_dns=True, np_allow_web=True)
    docs = export_k8s_docs(man, options=opts)
    np = next(d for d in docs if d.get("kind") == "NetworkPolicy")
    assert np["metadata"]["name"] == man.metadata.name
    spec = np["spec"]
    assert set(spec.get("policyTypes", [])) == {"Ingress", "Egress"}
    assert spec.get("ingress", []) == []
    egs = spec.get("egress", [])
    assert any(any(p.get("port") == 53 for p in e.get("ports", [])) for e in egs)
    assert any(any(p.get("port") == 80 for p in e.get("ports", [])) for e in egs)
    assert any(any(p.get("port") == 443 for p in e.get("ports", [])) for e in egs)
# ruff: noqa: E501
