from pathlib import Path

from ae.cli.__main__ import handle_export_k8s


def test_export_k8s_split_writes_files(tmp_path: Path) -> None:
    outdir = tmp_path / "out"
    from argparse import Namespace

    args = Namespace(
        file=Path("specs/examples/echo.yaml"),
        namespace="demo",
        ingress_class=None,
        service_port=None,
        workload="deployment",
        require_requests=False,
        output=None,
        out=None,
        split=outdir,
        emit_configs=False,
        inline_configs=False,
        emit_secrets=False,
        inline_secrets=False,
        emit_storage=False,
        default_pvc_size="1Gi",
        service_account=None,
        emit_pdb=False,
        pdb_min_available=None,
        pdb_max_unavailable=None,
        hpa_min=None,
        hpa_max=None,
        hpa_cpu_target=None,
        hpa_mem_target=None,
        hpa_mem_type=None,
        hpa_mem_value=None,
        allow_hpa_no_requests=False,
        default_security=False,
        preset=None,
        validate=False,
    )
    rc = handle_export_k8s(args)
    assert rc == 0
    files = sorted(p.name for p in outdir.glob("*.yaml"))
    # Expect at least deployment/service/ingress files
    assert any("deployment-echo" in f for f in files)
    assert any("service-echo" in f for f in files)
    assert any("ingress-echo" in f for f in files)
