from ae.k8s.convert import ingress_spec_from_k8s


def test_convert_ingress_annotations() -> None:
    ing = {
        "metadata": {
            "name": "demo",
            "namespace": "default",
            "annotations": {"nginx.ingress.kubernetes.io/rewrite-target": "/"},
        },
        "spec": {
            "rules": [
                {
                    "host": "demo.local",
                    "http": {
                        "paths": [
                            {
                                "path": "/",
                                "backend": {"service": {"name": "demo-svc"}},
                            }
                        ]
                    },
                }
            ]
        },
    }
    svc_map = {("default", "demo-svc"): ("default", "demo")}
    res = ingress_spec_from_k8s(ing, svc_map)
    assert res is not None
    _key, spec = res
    assert spec.annotations is not None
    assert spec.annotations.get("nginx.ingress.kubernetes.io/rewrite-target") == "/"
