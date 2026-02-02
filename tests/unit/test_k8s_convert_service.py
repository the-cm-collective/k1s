from ae.k8s.convert import service_spec_from_k8s


def test_service_spec_nodeport_preserves_service_port() -> None:
    svc = {
        "spec": {
            "type": "NodePort",
            "ports": [
                {
                    "name": "http",
                    "port": 80,
                    "targetPort": 8080,
                    "nodePort": 32000,
                    "protocol": "TCP",
                }
            ],
        }
    }
    spec = service_spec_from_k8s(svc, ports_by_name={"http": 8080})
    assert spec is not None
    assert spec.type == "NodePort"
    assert spec.port is None
    assert spec.ports
    port = spec.ports[0]
    assert port.port == 80
    assert port.target_port == 8080
    assert port.node_port == 32000
