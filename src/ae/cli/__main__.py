"""Command-line interface for the ae orchestrator."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Callable

from ae.controller.health import HealthManager
from ae.controller.reconciler import ReconcileReport, Reconciler
from ae.controller.state import AppStatus, SQLiteStateStore, RevisionInfo
from ae.ingress import CaddyIngressManager, IngressService
from ae.observability import MetricsService
from ae.observability.logging import configure_logging
from ae.runtime import (
    DockerRuntime,
    PodmanRuntime,
    RegistryAuthProvider,
    RuntimeAdapter,
    StubRuntime,
)
import logging, shutil
from ae.secrets import SecretManager
from ae.config.manager import ConfigManager
from ae import __version__ as AE_VERSION
from ae import build_info as AE_BUILD_INFO
from ae.k8s.exporter import ExportOptions, export_k8s_yaml
from ae.k8s.check import k8s_portability_issues
from ae.k8s.presets import apply_preset
from ae.k8s.validate import validate_documents


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ae", description="Minimal application engine CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)
    parser.add_argument("--verbose", action="store_true", help="Enable DEBUG logging")
    parser.add_argument(
        "--log-level", default=None, help="Override log level (DEBUG/INFO/WARNING/ERROR)"
    )
    parser.add_argument(
        "--server", default=None, help="Remote API base URL (e.g. http://127.0.0.1:9108)"
    )
    parser.add_argument("--token", default=None, help="Bearer token for remote API auth")

    apply_parser = subparsers.add_parser("apply", help="Apply a manifest")
    apply_parser.add_argument("-f", "--file", type=Path, required=True, help="Path to manifest")

    status_parser = subparsers.add_parser("status", help="Show application status")
    status_parser.add_argument("name", nargs="?", help="Application name (omit to list all)")
    status_parser.add_argument(
        "--history", type=int, default=0, help="Show the most recent N probe evaluations"
    )
    status_parser.add_argument(
        "--events", action="store_true", help="Show recent events alongside status"
    )
    status_parser.add_argument(
        "--wide", action="store_true", help="Show additional details like resources and volumes"
    )
    status_parser.add_argument("--json", action="store_true", help="Emit JSON instead of text")

    logs_parser = subparsers.add_parser("logs", help="Tail application logs")
    logs_parser.add_argument("name", help="Application name")
    logs_parser.add_argument("--follow", action="store_true", help="Stream logs continuously")
    logs_parser.add_argument(
        "--container", help="Replica selector: index (e.g. 0) or replica id", default=None
    )
    logs_parser.add_argument("--revision", type=int, default=None, help="Filter by revision number")
    logs_parser.add_argument(
        "--tail", type=int, default=None, help="Number of lines from the end of the logs"
    )
    logs_parser.add_argument(
        "--since",
        default=None,
        help="Only return logs newer than a relative duration like 5m, 1h (or seconds)",
    )
    logs_parser.add_argument(
        "--since-time",
        dest="since_time",
        default=None,
        help="Only return logs after an absolute timestamp (RFC3339, e.g., 2025-10-23T12:00:00Z)",
    )

    # exec: run a command inside a container
    exec_parser = subparsers.add_parser("exec", help="Run a command in a container")
    exec_parser.add_argument("name", help="Application name")
    exec_parser.add_argument(
        "--container", required=False, help="Target container name or replica id"
    )
    exec_parser.add_argument("--timeout", type=int, default=None, help="Timeout seconds")
    exec_parser.add_argument("cmd", nargs=argparse.REMAINDER, help="Command to execute after --")

    rollback_parser = subparsers.add_parser("rollback", help="Rollback an application revision")
    rollback_parser.add_argument("name", help="Application name")
    rollback_parser.add_argument(
        "--to",
        type=int,
        default=None,
        help="Target revision number (default: previous revision)",
    )

    revisions_parser = subparsers.add_parser("revisions", help="List stored revisions")
    revisions_parser.add_argument("name", help="Application name")
    revisions_parser.add_argument("--limit", type=int, default=10)

    # registry helpers
    registry_parser = subparsers.add_parser("registry", help="Manage registry credentials")
    reg_sub = registry_parser.add_subparsers(dest="registry_cmd", required=True)
    reg_list = reg_sub.add_parser("list", help="List configured registries")
    reg_login = reg_sub.add_parser("login", help="Login to a registry and store credentials")
    reg_login.add_argument(
        "provider", choices=["ghcr", "gcr", "ecr", "custom"], help="Registry provider"
    )
    reg_login.add_argument(
        "--registry", default=None, help="Registry hostname (defaults per provider)"
    )
    reg_login.add_argument(
        "--username", default=None, help="Username for basic auth (overrides provider defaults)"
    )
    reg_login.add_argument(
        "--password", default=None, help="Password/token for basic auth (overrides provider flows)"
    )
    reg_login.add_argument(
        "--token", default=None, help="Provider token (e.g., GHCR PAT or GCR access token)"
    )
    reg_login.add_argument(
        "--use-gcloud", action="store_true", help="Use 'gcloud auth print-access-token' for GCR"
    )
    reg_login.add_argument(
        "--gcr-host", default=None, help="GCR host override (e.g., us.gcr.io, eu.gcr.io)"
    )
    reg_login.add_argument(
        "--use-aws", action="store_true", help="Use 'aws ecr get-login-password' for ECR"
    )
    reg_login.add_argument("--region", default=None, help="AWS region (e.g., us-east-1)")
    reg_login.add_argument("--account-id", default=None, help="AWS account ID (12-digit)")
    reg_refresh = reg_sub.add_parser("refresh", help="Refresh short-lived tokens for registries")
    reg_refresh.add_argument(
        "--provider",
        choices=["all", "ghcr", "gcr", "ecr"],
        default="all",
        help="Provider to refresh (default: all)",
    )
    # kubesecret: render dockerconfigjson Secret
    reg_secret = reg_sub.add_parser(
        "kubesecret", help="Render a kubernetes.io/dockerconfigjson Secret from registries.yaml"
    )
    reg_secret.add_argument("--name", default="regcred", help="Secret name (default: regcred)")
    reg_secret.add_argument("--namespace", default="default", help="Namespace (default: default)")
    reg_secret.add_argument(
        "--host", action="append", default=[], help="Restrict to specific registry host(s); repeatable"
    )
    reg_secret.add_argument("--output", "-o", default="-", help="Output file ('-' for stdout)")

    metrics_parser = subparsers.add_parser("metrics", help="Show aggregated metrics")
    metrics_parser.add_argument("--json", action="store_true", help="Emit JSON output")

    events_parser = subparsers.add_parser("events", help="Show recent events")
    events_parser.add_argument("name", help="Application name")
    events_parser.add_argument("--limit", type=int, default=20)

    # config validate
    cfg_parser = subparsers.add_parser("config", help="Manage config resources")
    cfg_sub = cfg_parser.add_subparsers(dest="config_cmd", required=True)
    cfg_val = cfg_sub.add_parser("validate", help="Validate and show keys from a config file")
    cfg_val.add_argument("--file", "-f", type=Path, required=True)
    cfg_val.add_argument("--json", action="store_true", help="Emit JSON with keys")

    # secret validate
    sec_parser = subparsers.add_parser("secret", help="Manage secret resources")
    sec_sub = sec_parser.add_subparsers(dest="secret_cmd", required=True)
    sec_val = sec_sub.add_parser(
        "validate", help="Validate and show keys from a secret (SOPS) file"
    )
    sec_val.add_argument("--file", "-f", type=Path, required=True)
    sec_val.add_argument("--json", action="store_true", help="Emit JSON with keys")
    sec_enc = sec_sub.add_parser("encrypt", help="Encrypt a JSON/YAML file with sops (wrapper)")
    sec_enc.add_argument("--input", "-i", type=Path, required=True)
    sec_enc.add_argument("--output", "-o", type=Path, required=True)
    sec_dec = sec_sub.add_parser("decrypt", help="Decrypt a sops file (wrapper)")
    sec_dec.add_argument("--input", "-i", type=Path, required=True)
    sec_dec.add_argument("--output", "-o", type=Path, required=True)

    # delete <name> [--purge]
    delete_parser = subparsers.add_parser(
        "delete", help="Delete an application (containers + status)"
    )
    delete_parser.add_argument("name", help="Application name")
    delete_parser.add_argument(
        "--purge", action="store_true", help="Also purge events and revisions history"
    )

    # scale <name> --replicas N
    scale_parser = subparsers.add_parser(
        "scale", help="Scale an application by reconciling replicas"
    )
    scale_parser.add_argument("name", help="Application name")
    scale_parser.add_argument("--replicas", type=int, required=True)

    # backup/restore
    backup_parser = subparsers.add_parser("backup", help="Backup and restore state/specs")
    backup_sub = backup_parser.add_subparsers(dest="backup_cmd", required=True)

    backup_create = backup_sub.add_parser("create", help="Create a backup tar.gz of DB and specs")
    backup_create.add_argument("--output", required=True, help="Output tar.gz path")
    backup_create.add_argument("--db", default=None, help="Path to state DB (defaults AE_STATE_DB)")
    backup_create.add_argument(
        "--specs", default=None, help="Specs directory (defaults AE_SPECS_DIR or specs)"
    )

    backup_restore = backup_sub.add_parser(
        "restore", help="Restore a backup tar.gz into a directory"
    )
    backup_restore.add_argument("--input", required=True, help="Input tar.gz path")
    backup_restore.add_argument("--into", required=True, help="Target directory to extract into")

    backup_list = backup_sub.add_parser("list", help="List archive contents")
    backup_list.add_argument("--input", required=True, help="Input tar.gz path")

    backup_verify = backup_sub.add_parser("verify", help="Verify archive health and contents")
    backup_verify.add_argument("--input", required=True, help="Input tar.gz path")

    # version
    subparsers.add_parser("version", help="Show version and build info")

    # plan (dry-run scheduling/placement)
    plan = subparsers.add_parser("plan", help="Dry-run planner for manifest apply")
    plan.add_argument("-f", "--file", type=Path, required=True)
    plan.add_argument("--verbose", action="store_true", help="Show replica placement details")
    plan.add_argument("--strict", action="store_true", help="Treat warnings as errors")
    plan.add_argument("--json", action="store_true", help="Emit JSON instead of text")

    # export-k8s (render Kubernetes YAML)
    xk = subparsers.add_parser("export-k8s", help="Export a manifest to Kubernetes YAML")
    xk.add_argument("-f", "--file", type=Path, required=True)
    xk.add_argument("--namespace", default="default", help="K8s namespace (default: default)")
    xk.add_argument(
        "--ingress-class", default=None, help="Ingress class name (e.g., traefik or nginx)"
    )
    xk.add_argument(
        "--service-port", type=int, default=None, help="Override Service port (default: 80)"
    )
    xk.add_argument(
        "--workload",
        choices=["deployment", "statefulset"],
        default="deployment",
        help="Workload kind to emit (default: deployment)",
    )
    xk.add_argument(
        "--require-requests",
        action="store_true",
        help="Fail export if resources.requests (cpu and memory) are missing",
    )
    xk.add_argument("--output", "-o", type=Path, default=None, help="Write output to file path")
    xk.add_argument("--out", type=Path, default=None, help="Alias for --output")
    xk.add_argument(
        "--split",
        type=Path,
        default=None,
        help="Write each resource to its own YAML file in this directory",
    )
    xk.add_argument(
        "--emit-configs", action="store_true", help="Emit ConfigMap resources for configRefs"
    )
    xk.add_argument(
        "--inline-configs",
        action="store_true",
        help="Inline config file data into ConfigMaps (YAML/JSON)",
    )
    xk.add_argument(
        "--emit-secrets",
        action="store_true",
        help="Emit Secret resources for secretRefs (no data by default)",
    )
    xk.add_argument(
        "--inline-secrets",
        action="store_true",
        help="Inline secret data from plaintext YAML/JSON (use with caution)",
    )
    xk.add_argument(
        "--emit-storage", action="store_true", help="Emit PVCs from spec.storage and mount them"
    )
    xk.add_argument(
        "--default-pvc-size", default="1Gi", help="Default PVC size when storage.size is not set"
    )
    xk.add_argument(
        "--storage-class-name", default=None, help="PVC storageClassName to set on emitted PVCs"
    )
    xk.add_argument(
        "--service-account", default=None, help="Attach ServiceAccount and emit it by this name"
    )
    xk.add_argument(
        "--emit-pdb", action="store_true", help="Emit a PodDisruptionBudget when replicas > 1"
    )
    xk.add_argument(
        "--pdb-min-available", type=int, default=None, help="PDB minAvailable value (default: 1)"
    )
    xk.add_argument(
        "--pdb-max-unavailable",
        type=int,
        default=None,
        help="PDB maxUnavailable value (mutually exclusive with minAvailable)",
    )
    xk.add_argument(
        "--hpa-min",
        type=int,
        default=None,
        help="HPA min replicas (enables HPA with --hpa-max and --hpa-cpu-target)",
    )
    xk.add_argument("--hpa-max", type=int, default=None, help="HPA max replicas")
    xk.add_argument(
        "--hpa-cpu-target",
        type=int,
        default=None,
        help="HPA CPU averageUtilization target percent (e.g., 70)",
    )
    xk.add_argument(
        "--hpa-mem-target",
        type=int,
        default=None,
        help="HPA memory averageUtilization target percent",
    )
    xk.add_argument(
        "--hpa-mem-type",
        choices=["utilization", "value"],
        default=None,
        help="Memory HPA target type: utilization (percent) or value (e.g., 200Mi)",
    )
    xk.add_argument(
        "--hpa-mem-value",
        default=None,
        help="Memory HPA AverageValue quantity (e.g., 200Mi) when --hpa-mem-type=value",
    )
    xk.add_argument(
        "--hpa-behavior-up",
        default=None,
        help="JSON for autoscaling/v2 HPA scaleUp behavior (stabilizationWindowSeconds, policies)",
    )
    xk.add_argument(
        "--hpa-behavior-down",
        default=None,
        help="JSON for autoscaling/v2 HPA scaleDown behavior",
    )
    xk.add_argument(
        "--allow-hpa-no-requests",
        action="store_true",
        help="Allow HPA export without CPU/Memory requests (not recommended)",
    )
    xk.add_argument(
        "--default-security",
        action="store_true",
        help="Apply conservative securityContext defaults when none provided",
    )
    xk.add_argument(
        "--preset",
        choices=["web-basic", "web-hardened", "scale-ready", "web-strict"],
        default=None,
        help="Apply a preset of common flags",
    )
    xk.add_argument(
        "--validate", action="store_true", help="Validate generated YAML structure (offline checks)"
    )
    # NetworkPolicy helper flags
    xk.add_argument("--emit-np", action="store_true", help="Emit a default NetworkPolicy (deny-all per type)")
    xk.add_argument("--np-deny-ingress", action="store_true", help="Default deny ingress")
    xk.add_argument("--np-deny-egress", action="store_true", help="Default deny egress")
    xk.add_argument("--np-allow-dns", action="store_true", help="Allow DNS egress (TCP/UDP 53) when denying egress")
    xk.add_argument("--np-allow-web", action="store_true", help="Allow HTTP/HTTPS egress (TCP 80/443) when denying egress")
    xk.add_argument(
        "--np-preset",
        choices=["web", "backend"],
        default=None,
        help="Convenience network policy presets: web (DNS+HTTP/HTTPS), backend (DNS + internal DB/cache ports)",
    )
    xk.add_argument(
        "--np-allow-internal-port",
        action="append",
        default=[],
        dest="np_allow_internal_port",
        help="Allow egress to RFC1918 networks for this TCP port (repeatable)",
    )

    # Spread helper
    xk.add_argument(
        "--spread-by-host",
        action="store_true",
        help="Inject a basic topologySpreadConstraints across kubernetes.io/hostname when replicas>1",
    )

    # k8s-check (run FEAT checklist against manifest)
    kc = subparsers.add_parser("k8s-check", help="Run K8s portability checklist against a manifest")
    kc.add_argument("-f", "--file", type=Path, required=True)
    kc.add_argument(
        "--strict",
        action="store_true",
        help="Treat warnings as errors (deprecated; use --policy strict)",
    )
    kc.add_argument(
        "--policy",
        choices=["baseline", "strict"],
        default="baseline",
        help="Validation policy (strict escalates key warnings to errors)",
    )
    kc.add_argument("--json", action="store_true", help="Emit JSON output")
    kc.add_argument("--emit", action="store_true", help="Print the exported YAML used for checks")
    kc.add_argument("--kubeconform", action="store_true", help="Run kubeconform on exported YAML (if available)")
    kc.add_argument("--kubeconform-bin", default=os.getenv("KUBECONFORM_BIN", "kubeconform"))
    kc.add_argument("--kube-version", default=None, help="Set --kubernetes-version for kubeconform")
    kc.add_argument(
        "--assume-hpa",
        action="append",
        default=[],
        help="Assume HPA metrics when validating: cpu-util | mem-util | mem-value=<quantity> (repeatable)",
    )
    kc.add_argument(
        "--fail-on-warn",
        action="store_true",
        help="Return nonzero when any warnings are present (treat warns as failures)",
    )
    kc.add_argument(
        "--summary",
        action="store_true",
        help="Print a concise summary (counts + exit code legend)",
    )

    # k8s-report (generate compliance JSON for docs)
    kr = subparsers.add_parser("k8s-report", help="Generate a Kubernetes compliance report (JSON)")
    kr.add_argument(
        "--samples",
        nargs="+",
        default=[
            Path("specs/examples/echo.yaml"),
            Path("specs/examples/multi-replica-echo.yaml"),
            Path("specs/examples/echo-hpa.yaml"),
        ],
        help="List of App manifests to score (files)",
    )
    kr.add_argument("--namespace", default="demo")
    kr.add_argument(
        "--preset", choices=["web-basic", "web-hardened", "scale-ready"], default="web-hardened"
    )
    kr.add_argument("--ingress-class", default="traefik")
    kr.add_argument("--service-port", type=int, default=80)
    kr.add_argument("--output", "-o", type=Path, default=Path("docs/site/k8s_status.json"))
    kr.add_argument(
        "--run-dry-run",
        action="store_true",
        help="Attempt kubectl server-side dry-run if available",
    )
    kr.add_argument("--kubectl-bin", default=os.getenv("KUBECTL_BIN", "kubectl"))
    kr.add_argument("--kubeconform-bin", default=os.getenv("KUBECONFORM_BIN", "kubeconform"))
    kr.add_argument(
        "--apply-online",
        action="store_true",
        help="Apply exported YAML to the current cluster and wait for rollout",
    )
    kr.add_argument(
        "--cleanup", action="store_true", help="Delete applied resources after online test"
    )
    kr.add_argument(
        "--timeout", type=int, default=180, help="Rollout wait timeout seconds for online apply"
    )

    # rollout pause/resume
    rollout_cmd = subparsers.add_parser("rollout", help="Control rollout behavior (pause/resume)")
    rollout_sub = rollout_cmd.add_subparsers(dest="rollout_cmd", required=True)
    r_pause = rollout_sub.add_parser("pause", help="Pause rollout for an app")
    r_pause.add_argument("name", help="Application name")
    r_resume = rollout_sub.add_parser("resume", help="Resume rollout for an app")
    r_resume.add_argument("name", help="Application name")

    # api tokens helper
    api_cmd = subparsers.add_parser("api", help="HTTP API helpers")
    api_sub = api_cmd.add_subparsers(dest="api_cmd", required=True)
    api_tok = api_sub.add_parser("tokens", help="Generate or rotate bearer tokens for roles")
    api_tok.add_argument(
        "--generate", action="store_true", help="Generate random tokens and print export snippets"
    )
    api_tok.add_argument("--rotate", action="store_true", help="Rotate tokens (same as --generate)")
    api_tok.add_argument(
        "--output",
        "-o",
        type=Path,
        default=None,
        help="Write exports to this file instead of stdout",
    )
    api_tok.add_argument(
        "--ttl-hours",
        type=int,
        default=None,
        help="Optional hours until expiry; emits AE_API_*_TOKEN_EXPIRES lines (UTC)",
    )
    api_tok.add_argument(
        "--ttl-admin-hours", type=int, default=None, help="Override admin token expiry hours"
    )
    api_tok.add_argument(
        "--ttl-scaler-hours", type=int, default=None, help="Override scaler token expiry hours"
    )
    api_tok.add_argument(
        "--ttl-read-hours", type=int, default=None, help="Override read token expiry hours"
    )
    api_tok.add_argument(
        "--state",
        type=Path,
        default=None,
        help="Optional path to write JSON state with tokens and expiries",
    )

    # tls helpers
    tls_cmd = subparsers.add_parser("tls", help="TLS helpers for ingress")
    tls_sub = tls_cmd.add_subparsers(dest="tls_cmd", required=True)
    tls_sync = tls_sub.add_parser(
        "sync", help="Render PEMs from K8s Secret YAML/JSON or use direct files"
    )
    tls_sync.add_argument(
        "--name", "-n", required=True, help="Secret name (used as file prefix <name>.crt/.key)"
    )
    tls_sync.add_argument(
        "--input", "-i", type=Path, default=None, help="Path to K8s Secret YAML/JSON (optional)"
    )
    tls_sync.add_argument(
        "--root",
        type=Path,
        default=None,
        help="TLS root directory (defaults AE_TLS_DIR or state/tls)",
    )
    tls_verify = tls_sub.add_parser(
        "verify", help="Verify that TLS material for a name can be resolved"
    )
    tls_verify.add_argument("--name", "-n", required=True, help="Secret name to verify under root")
    tls_verify.add_argument(
        "--root",
        type=Path,
        default=None,
        help="TLS root directory (defaults AE_TLS_DIR or state/tls)",
    )
    tls_verify.add_argument("--json", action="store_true", help="Emit JSON output")

    # volumes list
    vols = subparsers.add_parser("volumes", help="Inspect storage volumes")
    vols_sub = vols.add_subparsers(dest="vol_cmd", required=True)
    vols_list = vols_sub.add_parser("list", help="List storage volumes (PV-lite)")
    vols_list.add_argument("--app", default=None, help="Filter by app name")
    vols_list.add_argument("--json", action="store_true", help="Emit JSON output")

    # examples helper
    ex = subparsers.add_parser("examples", help="Write example manifests")
    ex_sub = ex.add_subparsers(dest="ex_cmd", required=True)
    ex_write = ex_sub.add_parser("write", help="Write an example manifest to a file")
    ex_write.add_argument(
        "--type",
        choices=["multiport", "tcp-echo"],
        required=True,
        help="Example type to write",
    )
    ex_write.add_argument("--output", "-o", type=Path, required=True, help="Destination file path")
    ex_write.add_argument("--force", action="store_true", help="Overwrite existing file")

    # verify-image (cosign wrapper)
    vimg = subparsers.add_parser(
        "verify-image", help="Verify container image signatures with cosign"
    )
    vimg.add_argument("image", help="Image reference (e.g., ghcr.io/org/app:tag)")
    vimg.add_argument("--key", default=None, help="Path/URI to public key (cosign --key)")
    vimg.add_argument(
        "--certificate-identity",
        default=None,
        help="Certificate identity for keyless verification (cosign --certificate-identity)",
    )
    vimg.add_argument(
        "--certificate-oidc-issuer",
        default=None,
        help="OIDC issuer for keyless verification (cosign --certificate-oidc-issuer)",
    )
    vimg.add_argument(
        "--attachment",
        default=None,
        choices=["sbom"],
        help="Verify an attachment, e.g. sbom (cosign --attachment)",
    )
    vimg.add_argument("--rekor-url", default=None, help="Override Rekor URL")
    vimg.add_argument(
        "--cosign-bin", default=os.getenv("COSIGN_BIN", "cosign"), help="cosign binary path"
    )
    vimg.add_argument("--json", action="store_true", help="Emit JSON result")

    return parser


def state_store_from_env() -> SQLiteStateStore:
    db_path = Path(os.getenv("AE_STATE_DB", "state/controller.db"))
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return SQLiteStateStore(db_path)


def runtime_factory(registry_auth: RegistryAuthProvider | None = None) -> RuntimeAdapter:
    # Default to OCI/Podman; fall back to Docker when unavailable
    backend = os.getenv("AE_RUNTIME_BACKEND", "podman").lower()
    if backend == "stub":
        return StubRuntime()
    if backend in {"podman", "oci"}:
        try:
            # quick availability check
            if shutil.which(os.getenv("AE_PODMAN_BIN", "podman")) is None:
                raise RuntimeError("podman not found on PATH")
            return PodmanRuntime()
        except Exception as exc:
            logging.getLogger(__name__).warning(
                "Podman backend requested/unavailable (%s); falling back to Docker", exc
            )
            return DockerRuntime(registry_auth=registry_auth)
    return DockerRuntime(registry_auth=registry_auth)


def health_manager_factory() -> HealthManager:
    return HealthManager()


def _registry_config_path() -> Path:
    override = os.getenv("AE_REGISTRY_CONFIG")
    if override:
        return Path(override)
    return Path.home() / ".config" / "ae" / "registries.yaml"


def _registry_load() -> dict:
    import yaml as _yaml

    p = _registry_config_path()
    if not p.exists():
        return {}
    try:
        data = _yaml.safe_load(p.read_text()) or {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _registry_save(host: str, username: str, password: str) -> None:
    import yaml as _yaml

    p = _registry_config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    data = _registry_load()
    data[str(host)] = {"username": str(username), "password": str(password)}
    p.write_text(_yaml.safe_dump(data, sort_keys=True), encoding="utf-8")


def handle_registry(args: argparse.Namespace) -> int:
    cmd = getattr(args, "registry_cmd", None)
    if cmd == "kubesecret":
        import base64 as _b64
        import json as _json
        import yaml as _yaml
        prov = RegistryAuthProvider()
        entries = prov.list_registries()
        if not entries:
            print("no registries configured; use 'ae registry login' first")
            return 1
        selected = entries
        hosts = list(getattr(args, "host", []) or [])
        if hosts:
            selected = {h: entries[h] for h in hosts if h in entries}
            if not selected:
                print("no matching registry hosts found in config")
                return 2
        auths: dict[str, dict] = {}
        for host, cred in selected.items():
            u = cred.get("username") or ""
            p = cred.get("password") or ""
            auths[host] = {
                "username": u,
                "password": p,
                "auth": _b64.b64encode(f"{u}:{p}".encode()).decode(),
            }
        cfg = {"auths": auths}
        j = _json.dumps(cfg, separators=(",", ":"))
        b64 = _b64.b64encode(j.encode("utf-8")).decode("ascii")
        sec = {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": getattr(args, "name", "regcred"), "namespace": getattr(args, "namespace", "default")},
            "type": "kubernetes.io/dockerconfigjson",
            "data": {".dockerconfigjson": b64},
        }
        out = _yaml.safe_dump(sec, sort_keys=False)
        if getattr(args, "output", "-") in {"-", ""}:
            print(out, end="")
        else:
            from pathlib import Path as _P

            _P(str(args.output)).write_text(out, encoding="utf-8")
            print(f"wrote Secret to {args.output}")
        return 0
    if cmd == "list":
        prov = RegistryAuthProvider()
        entries = prov.list_registries()
        if not entries:
            print("no registries configured")
            return 0
        for host, creds in sorted(entries.items()):
            user = creds.get("username")
            print(f"{host}: {user}")
        return 0
    if cmd == "login":
        provider = str(getattr(args, "provider"))
        reg = getattr(args, "registry", None)
        user = getattr(args, "username", None)
        pw = getattr(args, "password", None)
        token = getattr(args, "token", None)
        # Provider-specific defaults
        if provider == "ghcr":
            host = reg or "ghcr.io"
            u = user or (os.getenv("GHCR_USERNAME") or os.getenv("USER") or "")
            # Try explicit password/token, then env, then gh CLI
            p = pw or token or os.getenv("GHCR_TOKEN") or os.getenv("GH_TOKEN") or ""
            if (not p) and shutil.which("gh") is not None:
                try:
                    import subprocess as sp

                    cp = sp.run(["gh", "auth", "token"], capture_output=True, text=True, check=True)
                    p = (cp.stdout or "").strip()
                except Exception:
                    p = p
            if not u or not p:
                print("error: --username and --token (or GHCR_TOKEN) required for ghcr")
                return 2
            _registry_save(host, u, p)
            print(f"saved credentials for {host} (user {u})")
            return 0
        if provider == "ecr":
            # AWS ECR short-lived password from AWS CLI
            host = reg
            region = (
                getattr(args, "region", None)
                or os.getenv("AWS_REGION")
                or os.getenv("AWS_DEFAULT_REGION")
            )
            account = getattr(args, "account_id", None) or os.getenv("AWS_ACCOUNT_ID")
            if not host:
                if not (region and account):
                    print(
                        "error: for ECR without --registry, provide --region and --account-id (or set AWS_REGION and AWS_ACCOUNT_ID)"
                    )
                    return 2
                host = f"{account}.dkr.ecr.{region}.amazonaws.com"
            if not region:
                # Try to infer region from registry host
                try:
                    parts = str(host).split(".")
                    region = parts[3]
                except Exception:
                    region = None
            if not region:
                print("error: unable to infer AWS region; pass --region or set AWS_REGION")
                return 2
            if not getattr(args, "use_aws", False) and not pw and not token:
                print(
                    "hint: pass --use-aws to obtain a short-lived password via AWS CLI, or provide --password/--token"
                )
            p = pw or token
            if not p and shutil.which("aws") is not None:
                try:
                    import subprocess as sp

                    cp = sp.run(
                        ["aws", "ecr", "get-login-password", "--region", region],
                        capture_output=True,
                        text=True,
                        check=True,
                    )
                    p = (cp.stdout or "").strip()
                except Exception as exc:  # noqa: BLE001
                    print(f"error: aws CLI failed to fetch ECR password: {exc}")
                    return 2
            if not p:
                print("error: missing ECR password; use --use-aws or provide --password/--token")
                return 2
            _registry_save(str(host), "AWS", p)
            print(f"saved credentials for {host} (user AWS)")
            return 0
        if provider == "gcr":
            host = getattr(args, "gcr_host", None) or reg or "gcr.io"
            if getattr(args, "use_gcloud", False) and not pw and not token:
                try:
                    import subprocess as sp

                    cp = sp.run(
                        ["gcloud", "auth", "print-access-token"],
                        capture_output=True,
                        text=True,
                        check=True,
                    )
                    token = (cp.stdout or "").strip()
                except Exception as exc:  # noqa: BLE001
                    print(f"error: gcloud failed to print access token: {exc}")
                    return 2
            p = pw or token or ""
            u = user or "oauth2accesstoken"
            if not p:
                print("error: missing --token/--password for gcr (or use --use-gcloud)")
                return 2
            _registry_save(host, u, p)
            print(f"saved credentials for {host} (user {u})")
            return 0
        # (no-op) duplicate ECR branch removed
        if provider == "custom":
            host = reg
            if not host or not user or not pw:
                print("error: custom requires --registry, --username, --password")
                return 2
            _registry_save(host, user, pw)
            print(f"saved credentials for {host} (user {user})")
            return 0
        print("unsupported provider")
        return 2
    if cmd == "refresh":
        prov_filter = getattr(args, "provider", "all")
        prov = RegistryAuthProvider()
        entries = prov.list_registries()
        if not entries:
            print("no registries configured; use 'ae registry login' first")
            return 1
        updated = 0
        for host, creds in sorted(entries.items()):
            try:
                if prov_filter in {"all", "ghcr"} and host == "ghcr.io":
                    new_tok = None
                    if shutil.which("gh") is not None:
                        import subprocess as sp

                        try:
                            cp = sp.run(
                                ["gh", "auth", "token"], capture_output=True, text=True, check=True
                            )
                            new_tok = (cp.stdout or "").strip()
                        except Exception:
                            new_tok = None
                    new_tok = new_tok or os.getenv("GHCR_TOKEN") or os.getenv("GH_TOKEN")
                    if new_tok:
                        user = (
                            creds.get("username")
                            or os.getenv("GHCR_USERNAME")
                            or os.getenv("USER")
                            or ""
                        )
                        _registry_save(host, user, new_tok)
                        print(f"refreshed ghcr token for {host}")
                        updated += 1
                        continue
                if prov_filter in {"all", "gcr"} and (host.endswith(".gcr.io") or host == "gcr.io"):
                    if shutil.which("gcloud") is not None:
                        import subprocess as sp

                        try:
                            cp = sp.run(
                                ["gcloud", "auth", "print-access-token"],
                                capture_output=True,
                                text=True,
                                check=True,
                            )
                            tok = (cp.stdout or "").strip()
                            if tok:
                                _registry_save(host, "oauth2accesstoken", tok)
                                print(f"refreshed gcr token for {host}")
                                updated += 1
                                continue
                        except Exception:
                            pass
                if prov_filter in {"all", "ecr"} and (
                    ".dkr.ecr." in host and host.endswith("amazonaws.com")
                ):
                    if shutil.which("aws") is not None:
                        import subprocess as sp

                        try:
                            region = host.split(".")[3]
                        except Exception:
                            region = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION")
                        if not region:
                            continue
                        try:
                            cp = sp.run(
                                ["aws", "ecr", "get-login-password", "--region", region],
                                capture_output=True,
                                text=True,
                                check=True,
                            )
                            pw_new = (cp.stdout or "").strip()
                            if pw_new:
                                _registry_save(host, "AWS", pw_new)
                                print(f"refreshed ecr password for {host}")
                                updated += 1
                                continue
                        except Exception:
                            pass
            except Exception:
                continue
        if updated == 0:
            print("no registry tokens refreshed")
            return 1
        return 0
    print("unsupported registry command")
    return 2


def ingress_service_factory() -> IngressService | None:
    root = os.getenv("AE_CADDY_SITES")
    if root is not None and root.strip() == "":
        return None
    config_root = Path(root) if root else Path("ops/dev/caddy/sites")
    config_root.mkdir(parents=True, exist_ok=True)

    binary = os.getenv("AE_CADDY_BIN", "caddy")
    config_file_env = os.getenv("AE_CADDY_FILE")
    config_file = Path(config_file_env) if config_file_env else None
    container = os.getenv("AE_CADDY_CONTAINER") or None
    container_cli = os.getenv("AE_CONTAINER_CLI", "docker")

    # Optional reload timeout to avoid hangs if docker exec blocks
    timeout_env = os.getenv("AE_CADDY_RELOAD_TIMEOUT", "10")
    try:
        reload_timeout = float(timeout_env) if timeout_env else None
    except ValueError:
        reload_timeout = 10.0

    manager = CaddyIngressManager(
        config_root=config_root,
        caddy_binary=binary,
        config_file=config_file,
        container=container,
        reload_timeout=reload_timeout,
        container_cli=container_cli,
    )
    return IngressService(manager)


def secret_manager_factory() -> SecretManager:
    allow_plaintext = os.getenv("AE_ALLOW_PLAINTEXT_SECRETS") == "1"
    return SecretManager(allow_plaintext=allow_plaintext)


def config_manager_factory() -> ConfigManager:
    return ConfigManager()


def registry_auth_factory() -> RegistryAuthProvider:
    return RegistryAuthProvider()


def format_report(report: ReconcileReport) -> str:
    return (
        f"Applied {report.app_name}: +{report.created}/~{report.updated}/-{report.removed}, "
        f"ready={report.ready_replicas}, live={report.live_replicas}, "
        f"rev={report.revision}({report.revision_status})"
    )


def format_status(status: AppStatus) -> str:
    parts = [
        f"{status.app_name}: desired={status.desired_replicas}",
        f"ready={status.ready_replicas}",
        f"live={status.live_replicas}",
        f"rev={status.revision}({status.revision_status})",
        f"image={status.image}",
        f"ops=+{status.created}/~{status.updated}/-{status.removed}",
    ]
    if status.ingress_host:
        path = status.ingress_path or "/"
        parts.append(f"ingress={status.ingress_host}{path}")
    return ", ".join(parts)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # Logging
    if args.verbose:
        configure_logging("DEBUG")
    elif args.log_level:
        configure_logging(args.log_level.upper())
    else:
        configure_logging(None)

    # Fast path for commands that don't need full wiring
    if args.command == "version":
        return handle_version()

    if args.command == "k8s-report":
        return handle_k8s_report(args)

    store = state_store_from_env()
    registry_auth = registry_auth_factory()
    runtime = runtime_factory(registry_auth=registry_auth)
    health_manager = health_manager_factory()
    ingress_service = ingress_service_factory()
    # Inject store into ingress service if supported (for canary state persistence)
    try:
        if ingress_service is not None:
            ingress_service._store = store  # type: ignore[attr-defined]
    except Exception:
        pass
    secret_manager = secret_manager_factory()
    config_manager = config_manager_factory()
    reconciler = Reconciler(
        runtime=runtime,
        state_store=store,
        health_manager=health_manager,
        ingress_service=ingress_service,
        secret_manager=secret_manager,
        config_manager=config_manager,
    )

    command_handlers: dict[str, Callable[[argparse.Namespace], int]] = {
        "apply": lambda ns: handle_apply(ns, reconciler, args),
        "status": lambda ns: handle_status(ns, store, args, runtime),
        "logs": lambda ns: handle_logs(ns, store, runtime),
        "exec": lambda ns: handle_exec(ns, store, runtime),
        "rollback": lambda ns: handle_rollback(ns, store, reconciler),
        "revisions": lambda ns: handle_revisions(ns, store),
        "rollout": lambda ns: handle_rollout(ns, store, reconciler),
        "api": handle_api,
        "tls": handle_tls,
        "registry": lambda ns: handle_registry(ns, registry_auth),
        "metrics": lambda ns: handle_metrics(ns, store),
        "events": lambda ns: handle_events(ns, store, args),
        "delete": lambda ns: handle_delete(ns, store, runtime, ingress_service, args),
        "scale": lambda ns: handle_scale(ns, store, reconciler, args),
        "backup": lambda ns: handle_backup(ns),
        "version": lambda ns: handle_version(),
        "config": lambda ns: handle_config(ns),
        "secret": lambda ns: handle_secret(ns),
        "volumes": lambda ns: handle_volumes(ns, runtime),
        "examples": handle_examples,
        "plan": lambda ns: handle_plan(ns, runtime),
        "export-k8s": handle_export_k8s,
        "k8s-check": handle_k8s_check,
        "registry": handle_registry,
        "verify-image": handle_verify_image,
    }

    handler = command_handlers.get(args.command)
    if handler is None:
        parser.error(f"Unhandled command: {args.command}")
        return 2
    return handler(args)


def handle_apply(
    args: argparse.Namespace, reconciler: Reconciler, global_args: argparse.Namespace | None = None
) -> int:
    # Remote apply via API when --server is set
    if global_args and getattr(global_args, "server", None):
        base = str(global_args.server)
        tok = getattr(global_args, "token", None)
        try:
            import yaml as _yaml

            payload = _yaml.safe_load(args.file.read_text())
        except Exception as exc:  # noqa: BLE001
            print(f"failed to read manifest: {exc}")
            return 1
        try:
            resp = _http_post_json(base, "/apply", payload, tok)
            print(
                f"applied {resp.get('app')} rev={resp.get('revision')}({resp.get('status')}) "
                f"ops=+{resp.get('created')}/~{resp.get('updated')}/-{resp.get('removed')}"
            )
            return 0
        except Exception as exc:  # noqa: BLE001
            print(f"remote apply failed: {exc}")
            return 1
    # Local apply path
    report = reconciler.reconcile_manifest_path(args.file)
    print(format_report(report))
    return 0


def handle_config(ns: argparse.Namespace) -> int:
    if ns.config_cmd == "validate":
        from ae.config.manager import ConfigManager

        mgr = ConfigManager()
        data = mgr._load(ns.file)  # internal, safe for CLI
        keys = sorted(list(data.keys()))
        if ns.json:
            import json as _json

            print(_json.dumps({"file": str(ns.file), "keys": keys}, indent=2))
        else:
            print(f"config keys in {ns.file}:")
            for k in keys:
                print(f"  - {k}")
        return 0
    print(f"Unsupported config command: {ns.config_cmd}")
    return 1


def handle_secret(ns: argparse.Namespace) -> int:
    if ns.secret_cmd == "validate":
        from ae.secrets.manager import SecretManager

        mgr = SecretManager()
        # Use SecretRef adapter to reuse decrypt
        from ae.controller.spec import SecretRef, SecretEnvMapping

        dummy = SecretRef(name="cli", path=str(ns.file), env=[SecretEnvMapping(name="_", key="_")])
        # Call decrypt privately to get mapping
        try:
            data = mgr._decrypt(ns.file)  # type: ignore[arg-type]
        except Exception as exc:  # noqa: BLE001
            print(f"decrypt failed: {exc}")
            return 1
        keys = sorted(list(map(str, data.keys())))
        if ns.json:
            import json as _json

            print(_json.dumps({"file": str(ns.file), "keys": keys}, indent=2))
        else:
            print(f"secret keys in {ns.file}:")
            for k in keys:
                print(f"  - {k}")
        return 0
    if ns.secret_cmd == "encrypt":
        # pass-through to sops -e -o
        from shutil import which

        sops = which("sops")
        if not sops:
            print("sops binary not found; install sops to use this wrapper")
            return 1
        import subprocess as sp

        try:
            sp.run([sops, "-e", "-o", str(ns.output), str(ns.input)], check=True)
            print(f"encrypted → {ns.output}")
            return 0
        except sp.CalledProcessError as exc:
            print(f"sops encrypt failed: {exc}")
            return 1
    if ns.secret_cmd == "decrypt":
        from shutil import which

        sops = which("sops")
        if not sops:
            print("sops binary not found; install sops to use this wrapper")
            return 1
        import subprocess as sp

        try:
            sp.run([sops, "-d", "-o", str(ns.output), str(ns.input)], check=True)
            print(f"decrypted → {ns.output}")
            return 0
        except sp.CalledProcessError as exc:
            print(f"sops decrypt failed: {exc}")
            return 1
    print(f"Unsupported secret command: {ns.secret_cmd}")
    return 1


def handle_delete(
    args: argparse.Namespace,
    store: SQLiteStateStore,
    runtime: RuntimeAdapter,
    ingress_service: IngressService | None,
    global_args: argparse.Namespace | None = None,
) -> int:
    if global_args and getattr(global_args, "server", None):
        base = str(global_args.server)
        tok = getattr(global_args, "token", None)
        try:
            resp = _http_post_json(
                base, f"/delete/{args.name}?purge={'1' if args.purge else '0'}", {}, tok
            )
            print(
                f"deleted {args.name}: removed={resp.get('removed', 0)} containers{' (purged history)' if resp.get('purged') else ''}"
            )
            return 0
        except Exception as exc:  # noqa: BLE001
            print(f"remote delete failed: {exc}")
            return 1
    name = args.name
    removed = runtime.remove_app(name)
    if ingress_service:
        try:
            ingress_service.remove(name)
            ingress_service.reload()
        except Exception:
            pass
    # If we have a manifest for the latest revision and purge requested, remove storage volumes with retention Delete
    if bool(args.purge):
        try:
            latest = store.list_revisions(name, limit=1)
            if latest:
                manifest = store.get_revision_manifest(name, latest[0].revision)
                deletes = [
                    s.name
                    for s in getattr(manifest.spec, "storage", [])
                    if str(getattr(s, "retention", "Retain")) == "Delete"
                ]
                if deletes:
                    try:
                        runtime.remove_storage_volumes(name, deletes)
                    except Exception:
                        pass
        except Exception:
            pass
    store.delete_app_state(name, purge_history=bool(args.purge))
    print(
        f"deleted {name}: removed={removed} containers{' (purged history)' if args.purge else ''}"
    )
    return 0


def handle_scale(
    args: argparse.Namespace,
    store: SQLiteStateStore,
    reconciler: Reconciler,
    global_args: argparse.Namespace | None = None,
) -> int:
    if global_args and getattr(global_args, "server", None):
        base = str(global_args.server)
        tok = getattr(global_args, "token", None)
        try:
            resp = _http_post_json(
                base, f"/scale/{args.name}", {"replicas": int(args.replicas)}, tok
            )
            print(
                f"scaled {args.name} to replicas={resp.get('replicas')} rev={resp.get('revision')}({resp.get('status')}) "
            )
            return 0
        except Exception as exc:  # noqa: BLE001
            print(f"remote scale failed: {exc}")
            return 1
    name = args.name
    latest = store.list_revisions(name, limit=1)
    if not latest:
        print(f"No revisions recorded for {name}. Try 'ae apply -f <manifest>'.")
        return 1
    manifest = store.get_revision_manifest(name, latest[0].revision)
    updated_spec = manifest.spec.model_copy(update={"replicas": int(args.replicas)})
    new_manifest = manifest.model_copy(update={"spec": updated_spec})
    report = reconciler.reconcile(new_manifest)
    print(
        f"scaled {name} to replicas={args.replicas}: rev={report.revision}({report.revision_status}) "
        f"ops=+{report.created}/~{report.updated}/-{report.removed} ready={report.ready_replicas}/{report.live_replicas}"
    )
    return 0


def handle_version() -> int:
    info = AE_BUILD_INFO()
    print(f"ae {AE_VERSION} ({info['sha']} {info['date']})")
    return 0


def handle_examples(args: argparse.Namespace) -> int:
    # Only supports 'write' for now
    if args.ex_cmd == "write":
        from shutil import copyfile

        src_map = {
            "multiport": Path("specs/examples/echo-multiport.yaml"),
            "tcp-echo": Path("specs/examples/tcp-echo.yaml"),
        }
        src = src_map.get(args.type)
        if src is None:
            print("unknown example type")
            return 2
        dst = Path(args.output)
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists() and not args.force:
            print(f"refusing to overwrite existing file: {dst} (use --force)")
            return 2
        try:
            copyfile(src, dst)
        except Exception as exc:  # noqa: BLE001
            print(f"failed to write example: {exc}")
            return 1
        print(f"wrote example to {dst}")
        return 0
    print("unsupported examples command")
    return 2


def handle_backup(args: argparse.Namespace) -> int:
    import os
    import tarfile
    from datetime import datetime

    def _resolve_db() -> str:
        if getattr(args, "db", None):
            return args.db
        return os.getenv("AE_STATE_DB", "state/controller.db")

    def _resolve_specs() -> str:
        val = getattr(args, "specs", None)
        if val:
            return val
        return os.getenv("AE_SPECS_DIR", "specs")

    if args.backup_cmd == "create":
        db_path = _resolve_db()
        specs_dir = _resolve_specs()
        out = args.output
        os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
        with tarfile.open(out, "w:gz") as tar:
            if os.path.exists(db_path):
                tar.add(db_path, arcname="state/controller.db")
            if os.path.isdir(specs_dir):
                tar.add(specs_dir, arcname="specs")
        print(f"backup written: {out}")
        return 0

    if args.backup_cmd == "restore":
        src = args.input
        target = args.into
        os.makedirs(target, exist_ok=True)
        with tarfile.open(src, "r:gz") as tar:
            # Prefer tarfile.data_filter when available to strip metadata
            data_filter = getattr(tarfile, "data_filter", None)
            for m in tar.getmembers():
                name = m.name
                # Skip absolute paths and path traversal
                from pathlib import Path as _P

                parts = _P(name).parts
                if name.startswith("/") or ".." in parts:
                    continue
                if data_filter is not None:
                    tar.extract(m, path=target, filter=data_filter)
                else:
                    tar.extract(m, path=target)
        print(f"backup restored into: {target}")
        return 0

    if args.backup_cmd == "list":
        src = args.input
        with tarfile.open(src, "r:gz") as tar:
            for m in tar.getmembers():
                print(m.name)
        return 0

    if args.backup_cmd == "verify":
        import io

        src = args.input
        with tarfile.open(src, "r:gz") as tar:
            names = set(m.name for m in tar.getmembers())
            ok = True
            # state db optional but recommended
            if "state/controller.db" not in names:
                print("warning: state/controller.db not found in archive")
            if not any(n.startswith("specs/") for n in names):
                print("error: specs/ directory missing in archive")
                ok = False
            # quick integrity read of small member
            for m in list(tar.getmembers())[:3]:
                _ = tar.extractfile(m)
        print("verify: ok" if ok else "verify: failed")
    return 0 if ok else 1

    print(f"Unsupported backup command: {args.backup_cmd}")
    return 1


def handle_k8s_report(args: argparse.Namespace) -> int:
    from datetime import datetime, timezone
    import json
    import shutil
    import tempfile
    from ae.controller.spec import load_manifest

    # Build export options (with preset)
    if getattr(args, "np_preset", None) == "web":
        setattr(args, "emit_np", True)
        setattr(args, "np_deny_ingress", True)
        setattr(args, "np_deny_egress", True)
        setattr(args, "np_allow_dns", True)
        setattr(args, "np_allow_web", True)
    elif getattr(args, "np_preset", None) == "backend":
        setattr(args, "emit_np", True)
        setattr(args, "np_deny_ingress", True)
        setattr(args, "np_deny_egress", True)
        setattr(args, "np_allow_dns", True)
    elif getattr(args, "np_preset", None) == "backend":
        setattr(args, "emit_np", True)
        setattr(args, "np_deny_ingress", True)
        setattr(args, "np_deny_egress", True)
        setattr(args, "np_allow_dns", True)
    opts = ExportOptions(
        namespace=str(args.namespace),
        ingress_class_name=str(args.ingress_class) if args.ingress_class else None,
        service_port=int(args.service_port) if args.service_port else None,
        default_security=True,
    )
    opts = apply_preset(opts, args.preset)

    kubeconform_bin = shutil.which(args.kubeconform_bin)
    kubectl_bin = (
        shutil.which(args.kubectl_bin) if (args.run_dry_run or args.apply_online) else None
    )

    results: list[dict] = []
    checks_total = 0
    checks_ran = 0
    sample_scores: list[float] = []

    for sample in args.samples:
        path = Path(sample)
        entry: dict = {"sample": str(path), "exists": path.exists()}
        if not path.exists():
            entry["error"] = "file not found"
            results.append(entry)
            continue

        try:
            manifest = load_manifest(path)
        except Exception as exc:  # noqa: BLE001
            entry["error"] = f"manifest load failed: {exc}"
            results.append(entry)
            continue

        # Export YAML
        yaml_text = export_k8s_yaml(manifest, options=opts)
        entry["export_length"] = len(yaml_text)

        # Offline validation
        valid_ok, errors = validate_documents(yaml_text)
        entry["validate"] = {"ran": True, "ok": bool(valid_ok), "errors": errors}

        # Policy issues (strict)
        issues = k8s_portability_issues(manifest)
        # Reuse apply_policy from check module to escalate in strict mode
        from ae.k8s.check import apply_policy

        strict = apply_policy(issues, "strict")
        err_count = sum(1 for i in strict if getattr(i, "level", "") == "error")
        warn_count = sum(1 for i in strict if getattr(i, "level", "") == "warn")
        entry["policy_strict"] = {"ran": True, "errors": err_count, "warnings": warn_count}

        # kubeconform (optional)
        kc_res = {"ran": False, "ok": None, "summary": None}
        if kubeconform_bin:
            kc_res["ran"] = True
            with tempfile.NamedTemporaryFile("w", delete=False) as tmp:
                tmp.write(yaml_text)
                tmp.flush()
                import subprocess as sp

                try:
                    proc = sp.run(
                        [kubeconform_bin, "-strict", "-summary", tmp.name],
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    kc_res["ok"] = proc.returncode == 0
                    kc_res["summary"] = proc.stdout.strip() or proc.stderr.strip()
                except Exception as exc:  # noqa: BLE001
                    kc_res["ok"] = False
                    kc_res["summary"] = f"kubeconform failed: {exc}"
        entry["kubeconform"] = kc_res

        # kubectl server-side dry-run (optional)
        dr_res = {"ran": False, "ok": None, "output": None}
        if kubectl_bin and args.run_dry_run:
            dr_res["ran"] = True
            with tempfile.NamedTemporaryFile("w", delete=False) as tmp:
                tmp.write(yaml_text)
                tmp.flush()
                import subprocess as sp

                try:
                    proc = sp.run(
                        [
                            kubectl_bin,
                            "apply",
                            "--dry-run=server",
                            "-f",
                            tmp.name,
                            "-n",
                            str(args.namespace),
                        ],
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    dr_res["ok"] = proc.returncode == 0
                    dr_res["output"] = (proc.stdout or proc.stderr).strip()
                except Exception as exc:  # noqa: BLE001
                    dr_res["ok"] = False
                    dr_res["output"] = f"kubectl dry-run failed: {exc}"
        entry["server_dry_run"] = dr_res

        # Summarize kinds emitted
        try:
            import yaml as _y

            kinds = []
            for d in _y.safe_load_all(yaml_text):
                if isinstance(d, dict):
                    kinds.append(str(d.get("kind")))
            entry["kinds"] = kinds
        except Exception:
            entry["kinds"] = []

        # Optional: online apply and rollout wait
        online = {"ran": False, "ok": None, "details": None}
        if kubectl_bin and args.apply_online:
            online["ran"] = True
            # Ensure namespace exists
            import subprocess as sp

            try:
                sp.run(
                    [kubectl_bin, "create", "namespace", str(args.namespace)],
                    capture_output=True,
                    text=True,
                )
            except Exception:
                pass
            with tempfile.NamedTemporaryFile("w", delete=False) as tmp:
                tmp.write(yaml_text)
                tmp.flush()
                try:
                    ap = sp.run(
                        [kubectl_bin, "apply", "-f", tmp.name, "-n", str(args.namespace)],
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    # Try to find the Deployment name(s) from parsed docs
                    deploys = [k for k in (entry.get("kinds") or []) if k == "Deployment"]
                    # Fallback: use manifest name
                    dep_name = manifest.metadata.name
                    # rollout status (best effort)
                    rs = sp.run(
                        [
                            kubectl_bin,
                            "rollout",
                            "status",
                            f"deploy/{dep_name}",
                            "-n",
                            str(args.namespace),
                            f"--timeout={int(args.timeout)}s",
                        ],
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    online["ok"] = ap.returncode == 0 and rs.returncode == 0
                    online["details"] = {
                        "apply_rc": ap.returncode,
                        "apply_out": (ap.stdout or ap.stderr).strip(),
                        "rollout_rc": rs.returncode,
                        "rollout_out": (rs.stdout or rs.stderr).strip(),
                    }
                except Exception as exc:  # noqa: BLE001
                    online["ok"] = False
                    online["details"] = {"error": str(exc)}
                finally:
                    if args.cleanup:
                        try:
                            sp.run(
                                [
                                    kubectl_bin,
                                    "delete",
                                    "-f",
                                    tmp.name,
                                    "-n",
                                    str(args.namespace),
                                    "--ignore-not-found",
                                ],
                                capture_output=True,
                                text=True,
                            )
                        except Exception:
                            pass
        entry["apply_online"] = online

        # Scoring per sample
        # Weights: validate=20, kubeconform=20, dry_run=30, policy_strict(no errors)=20, apply_online=10
        weights = {
            "validate": 20,
            "kubeconform": 20,
            "server_dry_run": 30,
            "policy_strict": 20,
            "apply_online": 10,
        }
        checks_total += sum(weights.values())
        ran_weight = 0
        got = 0
        if entry["validate"]["ran"]:
            ran_weight += weights["validate"]
            if entry["validate"]["ok"]:
                got += weights["validate"]
        if entry["kubeconform"]["ran"]:
            ran_weight += weights["kubeconform"]
            if bool(entry["kubeconform"]["ok"]):
                got += weights["kubeconform"]
        if entry["server_dry_run"]["ran"]:
            ran_weight += weights["server_dry_run"]
            if bool(entry["server_dry_run"]["ok"]):
                got += weights["server_dry_run"]
        if entry["policy_strict"]["ran"]:
            ran_weight += weights["policy_strict"]
            if int(entry["policy_strict"]["errors"]) == 0:
                got += weights["policy_strict"]
        if entry.get("apply_online", {}).get("ran"):
            ran_weight += weights["apply_online"]
            if bool(entry.get("apply_online", {}).get("ok")):
                got += weights["apply_online"]
        # Normalize to 100 for this sample based only on ran checks
        sample_score = (got / ran_weight * 100.0) if ran_weight else 0.0
        entry["score"] = round(sample_score, 1)
        entry["confidence"] = round(ran_weight / sum(weights.values()), 2)
        checks_ran += ran_weight
        sample_scores.append(sample_score)

        results.append(entry)

    overall = round(sum(sample_scores) / len(sample_scores), 1) if sample_scores else 0.0
    # Qualitative grade
    grade = "failing"
    if overall >= 90:
        grade = "excellent"
    elif overall >= 80:
        grade = "passing"
    elif overall >= 70:
        grade = "needs-attention"

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "samples_count": len(results),
        "overall_score": overall,
        "grade": grade,
        "results": results,
    }

    # Write JSON for docs server page consumption
    out: Path = args.output
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"wrote report → {out}")
    print(f"score={overall} grade={grade}")
    return 0


def _http_get_json(base: str, path: str, token: str | None = None):
    import requests

    url = base.rstrip("/") + path
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    r = requests.get(url, headers=headers, timeout=10)
    r.raise_for_status()
    return r.json()


def _http_post_json(base: str, path: str, body: dict, token: str | None = None):
    import requests

    url = base.rstrip("/") + path
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    r = requests.post(url, headers=headers, json=body, timeout=10)
    r.raise_for_status()
    return r.json()


def handle_status(
    args: argparse.Namespace, store: SQLiteStateStore, global_args: argparse.Namespace, runtime: RuntimeAdapter | None = None
) -> int:
    if getattr(global_args, "server", None):
        base = str(global_args.server)
        tok = getattr(global_args, "token", None)
        try:
            if args.name:
                path = f"/status/{args.name}"
                if args.wide:
                    path += "?details=1"
                data = _http_get_json(base, path, tok)
                print(
                    ", ".join(
                        [
                            f"{data['app_name']}: desired={data['desired_replicas']}",
                            f"ready={data['ready_replicas']}",
                            f"live={data['live_replicas']}",
                            f"rev={data['revision']}({data['revision_status']})",
                            f"image={data['image']}",
                        ]
                        + (
                            [f"ingress={data['ingress_host']}{data.get('ingress_path') or '/'}"]
                            if data.get("ingress_host")
                            else []
                        )
                    )
                )
                # When --wide, include replicas and containers details if available
                if args.wide:
                    try:
                        for r in data.get("replicas", []) or []:
                            print(
                                f"  - {r.get('replica_id')}: ready={bool(r.get('ready'))} "
                                f"live={bool(r.get('live'))} status={r.get('status')} | "
                                f"readiness={r.get('readiness_message')}; liveness={r.get('liveness_message')}"
                            )
                    except Exception:
                        pass
                    try:
                        conts = data.get("containers", []) or []
                        if conts:
                            print("  containers:")
                            for c in conts:
                                labs = c.get("labels") or {}
                                role = labs.get("ae.container", "")
                                rc = c.get("restart_count", 0)
                                print(f"    - {c.get('name')} role={role or 'main'} restarts={rc}")
                    except Exception:
                        pass
                return 0
            page = _http_get_json(base, "/status?limit=100", tok)
            for s0 in page.get("items", []):
                line = ", ".join(
                    [
                        f"{s0['app_name']}: desired={s0['desired_replicas']}",
                        f"ready={s0['ready_replicas']}",
                        f"live={s0['live_replicas']}",
                        f"rev={s0['revision']}({s0['revision_status']})",
                        f"image={s0['image']}",
                    ]
                    + (
                        [f"ingress={s0['ingress_host']}{s0.get('ingress_path') or '/'}"]
                        if s0.get("ingress_host")
                        else []
                    )
                )
                print(line)
            if page.get("next"):
                print(f"... next cursor: {page['next']}")
            return 0
        except Exception as exc:  # noqa: BLE001
            print(f"remote status failed: {exc}")
            return 1
    # local path
    if args.name:
        status = store.get_status(args.name)
        if status is None:
            print(f"No status recorded for {args.name}")
            return 1
        if args.json:
            print(_status_to_json(status, store, include_details=args.wide))
            return 0
        print(format_status(status))
        if args.wide:
            try:
                manifest = store.get_revision_manifest(args.name, status.revision)
                res = manifest.spec.resources
                vols = manifest.spec.volumes
                if res and res.limits:
                    cpu = res.limits.cpu if res.limits.cpu is not None else "-"
                    mem = res.limits.memory if res.limits.memory is not None else "-"
                    print(f"    resources: limits cpu={cpu}, memory={mem}")
                # Crashloop hint based on recent events
                try:
                    events = store.list_events(args.name, limit=10)
                    if any(e.event_type == "CrashLoopDetected" for e in events):
                        print(
                            "    crashloop: recent CrashLoopDetected events present (see 'ae events' for details)"
                        )
                except Exception:
                    pass
                if vols:
                    print(f"    volumes: {len(vols)} mounts")
            except Exception:
                pass
        replicas = store.list_replicas(args.name)
        for replica in replicas:
            print(
                f"  - {replica.replica_id}: ready={replica.ready} "
                f"live={replica.live} status={replica.status} | "
                f"readiness={replica.readiness_message}; "
                f"liveness={replica.liveness_message}"
            )
        if args.history and args.history > 0:
            history = store.get_probe_history(args.name, args.history)
            for entry in history:
                timestamp = entry.check_time.strftime("%Y-%m-%d %H:%M:%S")
                print(
                    f"    history {timestamp} {entry.replica_id}: ready={entry.ready} "
                    f"live={entry.live} | readiness={entry.readiness_message}; "
                    f"liveness={entry.liveness_message}"
                )
        if args.events:
            events = store.list_events(args.name, limit=10)
            if not events:
                print("    no events recorded")
            else:
                for event in events:
                    timestamp = event.created_at.strftime("%Y-%m-%d %H:%M:%S")
                    print(
                        f"    event {timestamp} rev={event.revision} "
                        f"{event.event_type}: {event.message}"
                    )
        # When --wide, include per-container runtime info if runtime is available
        if args.wide and runtime is not None:
            try:
                infos = runtime.list_containers_info()  # type: ignore[attr-defined]
            except Exception:
                infos = []
            if infos:
                filtered = [c for c in infos if (c.get('labels') or {}).get('ae.app') == args.name]
                if filtered:
                    print("  containers:")
                    for c in filtered:
                        labs = c.get("labels") or {}
                        role = labs.get("ae.container", "") or "main"
                        ports = ",".join(str(p) for p in (c.get("host_ports") or []))
                        print(
                            f"    - {c.get('name')} role={role} restarts={int(c.get('restart_count', 0))}"
                            + (f" ports=[{ports}]" if ports else "")
                        )
        return 0
    statuses = store.list_status()
    if not statuses:
        print("No applications recorded.")
        return 0
    if args.json:
        import json

        def as_dict(s: AppStatus) -> dict:
            d = {
                "app_name": s.app_name,
                "desired_replicas": s.desired_replicas,
                "ready_replicas": s.ready_replicas,
                "live_replicas": s.live_replicas,
                "revision": s.revision,
                "revision_status": s.revision_status,
                "image": s.image,
                "ingress_host": s.ingress_host,
                "ingress_path": s.ingress_path,
            }
            return d

        print(json.dumps([as_dict(s) for s in statuses], indent=2))
        return 0
    for status in statuses:
        print(format_status(status))
    return 0


def handle_rollout(
    args: argparse.Namespace, store: SQLiteStateStore, reconciler: Reconciler
) -> int:
    if args.rollout_cmd not in {"pause", "resume"}:
        print("unsupported rollout command")
        return 2
    app = args.name
    revs = store.list_revisions(app, limit=1)
    if not revs:
        print(f"no revisions recorded for {app}")
        return 1
    man = store.get_revision_manifest(app, revs[0].revision)
    rollout = dict(getattr(man.spec, "rollout", {}) or {})
    rollout["pause"] = True if args.rollout_cmd == "pause" else False
    new_spec = man.spec.model_copy(update={"rollout": rollout})
    updated = man.model_copy(update={"spec": new_spec})
    report = reconciler.reconcile(updated)
    print(
        f"rollout {args.rollout_cmd} {app}: rev={report.revision} status={report.revision_status} ready={report.ready_replicas}/{new_spec.replicas}"
    )
    return 0


def handle_api(args: argparse.Namespace) -> int:
    if args.api_cmd == "tokens" and (
        getattr(args, "generate", False) or getattr(args, "rotate", False)
    ):
        import secrets
        from datetime import datetime, timedelta, timezone

        admin = secrets.token_hex(16)
        scaler = secrets.token_hex(16)
        reader = secrets.token_hex(16)
        lines = [
            "# Add these to your environment (or CI secrets):",
            f"export AE_API_ADMIN_TOKEN={admin}",
            f"export AE_API_SCALER_TOKEN={scaler}",
            f"export AE_API_READ_TOKEN={reader}",
            "# To enable mutations:",
            "export AE_API_MUTATIONS=1",
        ]

        def _exp(hours):
            return (
                (datetime.now(timezone.utc) + timedelta(hours=int(hours)))
                .isoformat()
                .replace("+00:00", "Z")
            )

        ttl = getattr(args, "ttl_hours", None)
        admin_ttl = getattr(args, "ttl_admin_hours", None) or ttl
        scaler_ttl = getattr(args, "ttl_scaler_hours", None) or ttl
        read_ttl = getattr(args, "ttl_read_hours", None) or ttl
        admin_exp = _exp(admin_ttl) if admin_ttl and admin_ttl > 0 else None
        scaler_exp = _exp(scaler_ttl) if scaler_ttl and scaler_ttl > 0 else None
        read_exp = _exp(read_ttl) if read_ttl and read_ttl > 0 else None
        if admin_exp:
            lines.append(f"export AE_API_ADMIN_TOKEN_EXPIRES={admin_exp}")
        if scaler_exp:
            lines.append(f"export AE_API_SCALER_TOKEN_EXPIRES={scaler_exp}")
        if read_exp:
            lines.append(f"export AE_API_READ_TOKEN_EXPIRES={read_exp}")
        out = "\n".join(lines) + "\n"
        dest = getattr(args, "output", None)
        if dest:
            Path(dest).write_text(out)
        else:
            print(out, end="")
        # Optional JSON state
        state_path = getattr(args, "state", None)
        if state_path:
            import json as _json

            payload = {
                "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "admin": {"token": admin, "expires": admin_exp},
                "scaler": {"token": scaler, "expires": scaler_exp},
                "read": {"token": reader, "expires": read_exp},
            }
            Path(state_path).parent.mkdir(parents=True, exist_ok=True)
            Path(state_path).write_text(_json.dumps(payload, indent=2))
        return 0
    print("unsupported api command")
    return 2


def handle_tls(args: argparse.Namespace) -> int:
    # tls sync: copy optional input and resolve to PEM
    if args.tls_cmd == "sync":
        from ae.ingress.tls_sync import TlsSecretResolver
        import os

        root = Path(args.root) if args.root else Path(os.getenv("AE_TLS_DIR", "state/tls"))
        root.mkdir(parents=True, exist_ok=True)
        name = str(args.name)
        if getattr(args, "input", None):
            src: Path = args.input
            ext = src.suffix.lower().lstrip(".") or "yaml"
            if ext not in {"yaml", "yml", "json"}:
                ext = "yaml"
            dst = root / f"{name}.{ext}"
            try:
                dst.write_bytes(src.read_bytes())
            except Exception as exc:  # noqa: BLE001
                print(f"failed to copy secret file: {exc}")
                return 1
        resolver = TlsSecretResolver(root)
        resolved = resolver.resolve(name)
        if not resolved:
            print(
                "no TLS material found; provide --input or place <name>.crt/.key or <name>.yaml in root"
            )
            return 2
        crt, key = resolved
        print(f"TLS ready: cert={crt} key={key}")
        return 0
    # tls verify: check resolvability only
    if args.tls_cmd == "verify":
        from ae.ingress.tls_sync import TlsSecretResolver
        import os

        root = Path(args.root) if args.root else Path(os.getenv("AE_TLS_DIR", "state/tls"))
        resolver = TlsSecretResolver(root)
        resolved = resolver.resolve(str(args.name))
        if getattr(args, "json", False):
            import json as _json

            payload = {
                "name": str(args.name),
                "root": str(root),
                "ok": bool(resolved),
                "cert": str(resolved[0]) if resolved else None,
                "key": str(resolved[1]) if resolved else None,
            }
            print(_json.dumps(payload, indent=2))
            return 0 if resolved else 2
        if not resolved:
            print(f"not found: {args.name} under {root}")
            return 2
        crt, key = resolved
        print(f"found TLS: cert={crt} key={key}")
        return 0
    print("unsupported tls command")
    return 2


def handle_logs(args: argparse.Namespace, store: SQLiteStateStore, runtime: RuntimeAdapter) -> int:
    # Remote mode
    import inspect as _inspect

    frame = _inspect.currentframe()
    if frame is not None:
        outer_locals = frame.f_back.f_locals if frame.f_back else {}
        gargs = outer_locals.get("global_args") or outer_locals.get("args")
        if gargs is not None and getattr(gargs, "server", None):
            return handle_logs_remote(args, gargs)
    status = store.get_status(args.name)
    if status is None:
        print(f"No status recorded for {args.name}")
        return 1
    replicas = store.list_replicas(args.name)
    if not replicas:
        print(f"No replicas available for {args.name}")
        return 1
    # optional revision filter
    if args.revision is not None:
        rev_tag = f"-rev{args.revision}-"
        replicas = [r for r in replicas if rev_tag in r.replica_id]
        if not replicas:
            print(f"No replicas for {args.name} at revision {args.revision}")
            return 1

    # select by container flag
    target = None
    if args.container is not None:
        sel = str(args.container)
        if sel.isdigit():
            # match by replica index suffix
            suffix = f"-{sel}"
            for r in replicas:
                if r.replica_id.endswith(suffix):
                    target = r
                    break
        else:
            # exact match or contains
            for r in replicas:
                if r.replica_id == sel or sel in r.replica_id:
                    target = r
                    break
        if target is None:
            print(f"No matching replica for --container={sel}")
            return 1
    else:
        # prefer a ready replica, otherwise first
        target = next((r for r in replicas if r.ready), replicas[0])

    since_seconds = _parse_since_secs(args.since) if args.since else None
    if since_seconds is None and args.since_time:
        since_seconds = _parse_rfc3339_to_epoch(args.since_time)

    for line in runtime.read_logs(
        target.replica_id,
        follow=args.follow,
        tail=args.tail,
        since=since_seconds,
    ):
        print(line)
    return 0


def handle_exec(args: argparse.Namespace, store: SQLiteStateStore, runtime: RuntimeAdapter) -> int:
    # Remote mode
    import inspect as _inspect

    frame = _inspect.currentframe()
    if frame is not None:
        outer_locals = frame.f_back.f_locals if frame.f_back else {}
        gargs = outer_locals.get("global_args") or outer_locals.get("args")
        if gargs is not None and getattr(gargs, "server", None):
            return handle_exec_remote(args, gargs)

    # Local-only path
    status = store.get_status(args.name)
    if status is None:
        print(f"No status recorded for {args.name}")
        return 1
    replicas = store.list_replicas(args.name)
    if not replicas:
        print(f"No replicas available for {args.name}")
        return 1
    timeout = getattr(args, "timeout", None)
    cmd = list(getattr(args, "cmd", []) or [])
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        print("exec requires a command after -- (e.g., ae exec app --container sidecar -- sh -c 'echo hi')")
        return 2
    if getattr(args, "container", None):
        cname = str(args.container)
        # If runtime supports container-scoped exec, use it
        if hasattr(runtime, "exec_for_container"):
            try:
                rc = int(getattr(runtime, "exec_for_container")(args.name, cname, cmd, timeout=timeout))
                return rc
            except Exception as exc:  # noqa: BLE001
                print(f"exec failed: {exc}")
                return 1
        # Fallback: select a replica by id substring
        target = next((r for r in replicas if (r.replica_id == cname or cname in r.replica_id)), None)
        if not target:
            print(f"No matching replica for --container={cname}")
            return 1
        return int(runtime.exec(target.replica_id, cmd, timeout=timeout))
    # Default: exec in a ready replica (main container context)
    target = next((r for r in replicas if r.ready), replicas[0])
    return int(runtime.exec(target.replica_id, cmd, timeout=timeout))


def handle_exec_remote(args: argparse.Namespace, global_args: argparse.Namespace) -> int:
    base = str(global_args.server)
    tok = getattr(global_args, "token", None)
    payload = {
        "container": getattr(args, "container", None),
        "cmd": [str(x) for x in (getattr(args, "cmd", []) or []) if x != "--"],
    }
    if getattr(args, "timeout", None) is not None:
        payload["timeoutSeconds"] = int(args.timeout)
    import requests

    url = base.rstrip("/") + "/exec/" + args.name
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=15)  # type: ignore
        resp.raise_for_status()
        data = resp.json()
        rc = int(data.get("rc", 1))
        # Print nothing on success to be script-friendly
        return rc
    except Exception as exc:  # noqa: BLE001
        print(f"remote exec failed: {exc}")
        return 1


def _status_to_json(status: AppStatus, store: SQLiteStateStore, *, include_details: bool) -> str:
    import json

    data = {
        "app_name": status.app_name,
        "desired_replicas": status.desired_replicas,
        "ready_replicas": status.ready_replicas,
        "live_replicas": status.live_replicas,
        "revision": status.revision,
        "revision_status": status.revision_status,
        "image": status.image,
        "ingress_host": status.ingress_host,
        "ingress_path": status.ingress_path,
    }
    if include_details:
        try:
            manifest = store.get_revision_manifest(status.app_name, status.revision)
            res = manifest.spec.resources
            vols = manifest.spec.volumes
            if res and res.limits:
                data["resources"] = {
                    "limits": {
                        "cpu": res.limits.cpu,
                        "memory": res.limits.memory,
                    }
                }
            if vols:
                data["volumes"] = [
                    {
                        "hostPath": v.host_path,
                        "mountPath": v.mount_path,
                        "readOnly": v.read_only,
                    }
                    for v in vols
                ]
        except Exception:
            pass
    return json.dumps(data, indent=2)


def _parse_since_secs(value: str | None) -> int | None:
    if value is None:
        return None
    v = value.strip().lower()
    if v.isdigit():
        return int(v)
    try:
        # simple units: s, m, h
        num = ""
        unit = "s"
        for ch in v:
            if ch.isdigit():
                num += ch
            else:
                unit = ch
        n = int(num) if num else 0
        if unit == "h":
            return n * 3600
        if unit == "m":
            return n * 60
        return n
    except Exception:
        return None


def _parse_rfc3339_to_epoch(value: str | None) -> int | None:
    if not value:
        return None
    try:
        import datetime as _dt
        from datetime import timezone as _tz

        s = value.strip()
        # Support trailing Z or offset like +00:00
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = _dt.datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_tz.utc)
        return int(dt.timestamp())
    except Exception:
        return None


def handle_rollback(
    args: argparse.Namespace,
    store: SQLiteStateStore,
    reconciler: Reconciler,
) -> int:
    target_rev: int | None = args.to
    if target_rev is None:
        revisions = store.list_revisions(args.name, limit=2)
        if len(revisions) < 2:
            print("No previous revision to roll back to.")
            return 1
        target_rev = revisions[1].revision

    try:
        manifest = store.get_revision_manifest(args.name, target_rev)
    except ValueError as exc:
        print(str(exc))
        return 1

    report = reconciler.reconcile(manifest)
    print(f"Rolled back {args.name} to revision {report.revision} ({report.revision_status})")
    return 0


def handle_revisions(args: argparse.Namespace, store: SQLiteStateStore) -> int:
    revisions = store.list_revisions(args.name, limit=args.limit)
    if not revisions:
        print(f"No revisions recorded for {args.name}.")
        return 0
    for info in revisions:
        print(
            f"rev {info.revision}: status={info.status}, image={info.image}, "
            f"hash={info.spec_hash[:8]}"
        )
    return 0


## (removed duplicate handle_registry definition that conflicted with the primary one above)


def handle_metrics(args: argparse.Namespace, store: SQLiteStateStore) -> int:
    service = MetricsService(store)
    snapshot = service.snapshot()
    if args.json:
        import json

        print(
            json.dumps(
                {
                    "total_apps": snapshot.total_apps,
                    "ready_apps": snapshot.ready_apps,
                    "progressing_apps": snapshot.progressing_apps,
                    "degraded_apps": snapshot.degraded_apps,
                    "total_replicas": snapshot.total_replicas,
                    "ready_replicas": snapshot.ready_replicas,
                    "live_replicas": snapshot.live_replicas,
                },
                indent=2,
            )
        )
        return 0

    print(
        "apps total={total} ready={ready} progressing={progressing} degraded={degraded}".format(
            total=snapshot.total_apps,
            ready=snapshot.ready_apps,
            progressing=snapshot.progressing_apps,
            degraded=snapshot.degraded_apps,
        )
    )
    print(
        "replicas total={total} ready={ready} live={live}".format(
            total=snapshot.total_replicas,
            ready=snapshot.ready_replicas,
            live=snapshot.live_replicas,
        )
    )
    return 0


def handle_events(
    args: argparse.Namespace, store: SQLiteStateStore, global_args: argparse.Namespace
) -> int:
    if getattr(global_args, "server", None):
        base = str(global_args.server)
        tok = getattr(global_args, "token", None)
        limit = getattr(args, "limit", 20)
        try:
            page = _http_get_json(base, f"/events/{args.name}?limit={int(limit)}", tok)
            items = page.get("items", []) if isinstance(page, dict) else page
            if not items:
                print(f"No events recorded for {args.name}.")
                return 0
            for e in items:
                ts = e.get("created_at", "")
                print(f"{ts} rev={e.get('revision')} {e.get('event_type')}: {e.get('message')}")
            if isinstance(page, dict) and page.get("next"):
                print(f"... next cursor: {page['next']}")
            return 0
        except Exception as exc:  # noqa: BLE001
            print(f"remote events failed: {exc}")
            return 1
    events = store.list_events(args.name, limit=args.limit)
    if not events:
        print(f"No events recorded for {args.name}.")
        return 0
    for event in events:
        timestamp = event.created_at.strftime("%Y-%m-%d %H:%M:%S")
        print(f"{timestamp} rev={event.revision} {event.event_type}: {event.message}")
    return 0


def handle_volumes(args: argparse.Namespace, runtime: RuntimeAdapter) -> int:
    if args.vol_cmd == "list":
        try:
            vols = runtime.list_storage_volumes(getattr(args, "app", None))  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            print(f"volume listing not available: {exc}")
            return 1
        if not vols:
            print("No storage volumes found.")
            return 0
        if getattr(args, "json", False):
            import json as _json

            print(_json.dumps(vols, indent=2))
        else:
            for v in vols:
                name = v.get("name", "")
                labels = v.get("labels", {})
                drv = v.get("driver", "")
                mnt = v.get("mountpoint", "")
                app = labels.get("ae.app", "")
                print(f"{name} driver={drv} mount={mnt} app={app}")
        return 0
    print(f"Unsupported volumes command: {args.vol_cmd}")
    return 1


def handle_logs_remote(args: argparse.Namespace, global_args: argparse.Namespace) -> int:
    base = str(global_args.server)
    tok = getattr(global_args, "token", None)
    params = []
    if args.container:
        params.append(("container", str(args.container)))
    if args.tail is not None:
        params.append(("tail", str(int(args.tail))))
    if args.since is not None:
        since_secs = _parse_since_secs(args.since)
        if since_secs is not None:
            params.append(("since", str(int(since_secs))))
    if args.since_time:
        secs = _parse_rfc3339_to_epoch(args.since_time)
        if secs is not None:
            params.append(("since", str(int(secs))))
    if args.follow:
        params.append(("follow", "1"))
    from urllib.parse import urlencode

    path = f"/logs/{args.name}"
    if params:
        path += "?" + urlencode(params)
    import requests

    url = base.rstrip("/") + path
    headers = {"Accept": "text/plain" if args.follow else "application/json"}
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    try:
        if args.follow:
            with requests.get(url, headers=headers, stream=True, timeout=10) as r:  # type: ignore
                r.raise_for_status()
                for chunk in r.iter_lines(decode_unicode=True):
                    if chunk is None:
                        continue
                    print(chunk)
        else:
            resp = requests.get(url, headers=headers, timeout=10)  # type: ignore
            resp.raise_for_status()
            data = resp.json()
            for line in data.get("lines", []):
                print(line)
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"remote logs failed: {exc}")
        return 1


def handle_plan(args: argparse.Namespace, runtime: RuntimeAdapter) -> int:
    from ae.controller.spec import load_manifest

    try:
        manifest = load_manifest(args.file)
    except Exception as exc:  # noqa: BLE001
        print(f"failed to load manifest: {exc}")
        return 1

    desired = int(manifest.spec.replicas)
    rollout = getattr(manifest.spec, "rollout", {}) or {}
    strategy = str(rollout.get("strategy", "parallel"))
    if not getattr(args, "json", False):
        print(f"Plan for {manifest.metadata.name}:")
        print(
            f"  - replicas: {desired}\n  - rollout: strategy={strategy} maxSurge={rollout.get('maxSurge', 1)} maxUnavailable={rollout.get('maxUnavailable', 0)}"
        )

    warnings: list[str] = []
    diagnostics: dict = {"service": {}, "tls": {}}

    # Security hardening hints
    try:
        sec = getattr(manifest.spec, "security", None)
        if sec is None:
            warnings.append(
                "no security context provided; consider readOnlyRootFilesystem, dropCapabilities, and seccomp/AppArmor profiles"
            )
        else:
            if not bool(getattr(sec, "read_only_root", False)):
                warnings.append("readOnlyRootFilesystem is not enabled")
            drops = list(getattr(sec, "drop_caps", []) or [])
            if not drops:
                warnings.append(
                    "no Linux capabilities dropped; consider dropping NET_RAW and other unnecessary caps"
                )
            s_type = getattr(sec, "seccomp_type", None)
            if not s_type:
                warnings.append(
                    "no seccompProfileType set; RuntimeDefault is recommended (or Localhost with a custom profile)"
                )
            a_prof = getattr(sec, "apparmor_profile", None)
            if not a_prof:
                warnings.append(
                    "no apparmorProfile set; runtime/default is recommended on AppArmor-enabled hosts"
                )
    except Exception:
        # non-fatal; continue planning
        pass

    # Requests vs limits sanity checks
    try:
        res = getattr(manifest.spec, "resources", None)
        req = getattr(res, "requests", None) if res else None
        lim = getattr(res, "limits", None) if res else None
        if req and lim:
            try:
                if getattr(req, "cpu", None) is not None and getattr(lim, "cpu", None) is not None:
                    if float(req.cpu) > float(lim.cpu):
                        warnings.append("resources.requests.cpu exceeds limits.cpu")
            except Exception:
                pass
            try:
                # Compare memory quantities in a simple way when both are plain numbers with units of same suffix
                rq = getattr(req, "memory", None)
                lm = getattr(lim, "memory", None)
                if rq is not None and lm is not None and str(rq).strip() and str(lm).strip():
                    # Only compare when units match (best-effort)
                    import re as _re

                    m1 = _re.match(r"^(\d+(?:\.\d+)?)(.*)$", str(rq).strip())
                    m2 = _re.match(r"^(\d+(?:\.\d+)?)(.*)$", str(lm).strip())
                    if m1 and m2 and m1.group(2).strip().lower() == m2.group(2).strip().lower():
                        if float(m1.group(1)) > float(m2.group(1)):
                            warnings.append("resources.requests.memory exceeds limits.memory")
            except Exception:
                pass
    except Exception:
        pass

    svc = getattr(manifest.spec, "service", None)
    if svc and desired == 1:
        # Multi-port list takes precedence; otherwise check single service.port
        ports_to_check: list[int] = []
        # NodePort validation warning when exporting later
        if getattr(svc, "type", None) == "NodePort" and getattr(svc, "ports", None):
            name_seen: set[str] = set()
            port_seen: set[int] = set()
            nodeport_seen: set[int] = set()
            dup_names: list[str] = []
            dup_ports: list[int] = []
            dup_nodeports: list[int] = []
            out_of_range: list[int] = []
            for sp in svc.ports:
                np = getattr(sp, "node_port", None)
                if np is not None and not (30000 <= int(np) <= 32767):
                    warnings.append(
                        f"service.ports[{sp.name}].nodePort {np} is outside the default Kubernetes range 30000-32767"
                    )
                    out_of_range.append(int(np))
                # duplicate checks
                nm = getattr(sp, "name", None)
                if nm in name_seen:
                    warnings.append(f"duplicate service port name '{nm}'")
                    dup_names.append(str(nm))
                elif nm is not None:
                    name_seen.add(nm)
                try:
                    pnum = int(getattr(sp, "port", -1))
                    if pnum in port_seen:
                        warnings.append(f"duplicate service port {pnum}")
                        dup_ports.append(pnum)
                    else:
                        port_seen.add(pnum)
                except Exception:
                    pass
                if np is not None:
                    npi = int(np)
                    if npi in nodeport_seen:
                        warnings.append(f"duplicate service nodePort {npi}")
                        dup_nodeports.append(npi)
                    else:
                        nodeport_seen.add(npi)
            diagnostics["service"]["type"] = "NodePort"
            diagnostics["service"]["duplicates"] = {
                "names": dup_names,
                "ports": dup_ports,
                "nodePorts": dup_nodeports,
            }
            diagnostics["service"]["outOfRangeNodePorts"] = out_of_range
        if getattr(svc, "ports", None):
            try:
                ports_to_check = [
                    int(sp.port) for sp in svc.ports if getattr(sp, "port", None) is not None
                ]
            except Exception:
                ports_to_check = []
        elif getattr(svc, "port", None) is not None:
            ports_to_check = [int(svc.port)]
        if ports_to_check and not getattr(args, "json", False):
            print(f"  - checking service port(s) {ports_to_check} for conflicts...")
            conflicts: dict[int, list[str]] = {}
            try:
                infos = runtime.list_containers_info()  # type: ignore[attr-defined]
                for p in ports_to_check:
                    for info in infos:
                        if p in (info.get("host_ports") or []):
                            conflicts.setdefault(int(p), []).append(str(info.get("name", "")))
            except Exception:
                conflicts = {}
            if any(conflicts.values()):
                diagnostics["service"]["hostPortConflicts"] = conflicts
                if not getattr(args, "json", False):
                    for p, conts in conflicts.items():
                        if conts:
                            print(
                                f"  ! conflict: host port {p} is already published by: {', '.join(conts)}"
                            )
                    print(
                        "    Resolve by stopping the conflicting service(s) or changing spec.service.ports"
                    )
                if getattr(args, "json", False):
                    import json as _json

                    out = {
                        "app": manifest.metadata.name,
                        "replicas": desired,
                        "rollout": strategy,
                        "service": {"ports": ports_to_check},
                        "warnings": warnings,
                        "diagnostics": diagnostics,
                        "conflicts": conflicts,
                        "ok": False,
                    }
                    print(_json.dumps(out, indent=2))
                return 2
            if not getattr(args, "json", False):
                print("    OK: all requested host ports available")
    else:
        if (
            svc
            and desired != 1
            and (getattr(svc, "port", None) is not None or getattr(svc, "ports", None))
        ):
            print("  - note: service.port is only applied for single-replica apps in this version")
            warnings.append(
                "spec.service defined with replicas>1; stable host port is not applied for multi-replica apps. Use ingress for HTTP or per-replica ports."
            )

    # Materialize service.port details for JSON diagnostics
    if svc:
        try:
            if getattr(svc, "ports", None):
                diagnostics["service"]["ports"] = [
                    {
                        "name": getattr(sp, "name", None),
                        "port": int(getattr(sp, "port", 0) or 0),
                        "targetPort": getattr(sp, "target_port", None),
                        "nodePort": getattr(sp, "node_port", None),
                    }
                    for sp in svc.ports
                ]
            elif getattr(svc, "port", None) is not None:
                diagnostics["service"]["stablePort"] = {
                    "port": int(getattr(svc, "port", 0) or 0),
                    "targetPort": getattr(svc, "target_port", None),
                }
            if getattr(svc, "type", None):
                diagnostics["service"]["type"] = str(getattr(svc, "type"))
        except Exception:
            pass

    # Affinity warnings
    try:
        infos = runtime.list_containers_info()  # type: ignore[attr-defined]
    except Exception:
        infos = []
    running_same = [
        i for i in infos if (i.get("labels") or {}).get("ae.app") == manifest.metadata.name
    ]
    if running_same and not getattr(args, "json", False):
        print(
            f"  - note: found {len(running_same)} running container(s) for app '{manifest.metadata.name}' (rollout surge may temporarily increase count)"
        )
    if desired > 1 and not os.getenv("AE_DOCKER_NETWORK") and not getattr(args, "json", False):
        print(
            "  - note: AE_DOCKER_NETWORK is not set; multi-replica ingress may require host ports"
        )

    # Affinity warnings
    try:
        infos = runtime.list_containers_info()  # type: ignore[attr-defined]
    except Exception:
        infos = []
    running_same = [
        i for i in infos if (i.get("labels") or {}).get("ae.app") == manifest.metadata.name
    ]
    if running_same:
        warnings.append(
            f'found {len(running_same)} running container(s) for app "{manifest.metadata.name}" (surge may increase count)'
        )
    import os as _os

    if desired > 1 and not _os.getenv("AE_DOCKER_NETWORK"):
        warnings.append(
            "AE_DOCKER_NETWORK is not set; multi-replica ingress may require host ports"
        )
    # Podman shared network hint when using Podman backend
    backend = str(_os.getenv("AE_RUNTIME_BACKEND", "podman")).lower() or "podman"
    if desired > 1 and backend in {"podman", "oci"} and not _os.getenv("AE_PODMAN_NETWORK"):
        warnings.append(
            "AE_PODMAN_NETWORK is not set; for multi-replica ingress via container DNS, create a podman network and export AE_PODMAN_NETWORK=<name>"
        )

    # Rollout validation
    try:
        ms = int(rollout.get("maxSurge", 1))
        mu = int(rollout.get("maxUnavailable", 0))
        if ms < 0:
            warnings.append("rollout.maxSurge must be >= 0")
        if mu < 0:
            warnings.append("rollout.maxUnavailable must be >= 0")
        if desired > 0 and mu >= desired:
            warnings.append("rollout.maxUnavailable must be < replicas")
        if desired > 0 and ms > desired:
            warnings.append("rollout.maxSurge should not exceed replicas")
        # Hooks structure
        hooks = (rollout.get("hooks") or {}) if isinstance(rollout, dict) else {}
        if hooks:
            pre = hooks.get("preSwitch") if "preSwitch" in hooks else hooks.get("pre_switch")
            post = hooks.get("postSwitch") if "postSwitch" in hooks else hooks.get("post_switch")
            for name, h in (("preSwitch", pre), ("postSwitch", post)):
                if h is None:
                    continue
                if ("exec" not in h) and ("tcp" not in h):
                    warnings.append(f"rollout.hooks.{name} must contain 'exec' or 'tcp'")
                if "exec" in h and not isinstance(h.get("exec"), (list, tuple)):
                    warnings.append(f"rollout.hooks.{name}.exec must be a list of args")
                if "tcp" in h:
                    try:
                        port = int((h.get("tcp") or {}).get("port", -1))
                        if port <= 0 or port > 65535:
                            warnings.append(f"rollout.hooks.{name}.tcp.port must be 1-65535")
                    except Exception:
                        warnings.append(f"rollout.hooks.{name}.tcp.port must be an integer")
    except Exception:
        pass

    # Private registry creds check
    try:
        img = str(getattr(manifest.spec, "image", ""))
        host = img.split("/", 1)[0] if "." in img.split("/", 1)[0] else None
        if host:
            from ae.runtime.registry import RegistryAuthProvider as _R

            prov = _R()
            creds = prov.list_registries().get(host)
            if not creds:
                warnings.append(
                    f"registry credentials for '{host}' not found; run 'ae registry login' or configure ~/.config/ae/registries.yaml"
                )
    except Exception:
        pass

    # Rollout validation
    try:
        ms = int(rollout.get("maxSurge", 1))
        mu = int(rollout.get("maxUnavailable", 0))
        if ms < 0:
            warnings.append("rollout.maxSurge must be >= 0")
        if mu < 0:
            warnings.append("rollout.maxUnavailable must be >= 0")
        if desired > 0 and mu >= desired:
            warnings.append("rollout.maxUnavailable must be < replicas")
        if desired > 0 and ms > desired:
            warnings.append("rollout.maxSurge should not exceed replicas")
        # Hooks structure
        hooks = (rollout.get("hooks") or {}) if isinstance(rollout, dict) else {}
        if hooks:
            pre = hooks.get("preSwitch") if "preSwitch" in hooks else hooks.get("pre_switch")
            post = hooks.get("postSwitch") if "postSwitch" in hooks else hooks.get("post_switch")
            for name, h in (("preSwitch", pre), ("postSwitch", post)):
                if h is None:
                    continue
                if ("exec" not in h) and ("tcp" not in h):
                    warnings.append(f"rollout.hooks.{name} must contain 'exec' or 'tcp'")
                if "exec" in h and not isinstance(h.get("exec"), (list, tuple)):
                    warnings.append(f"rollout.hooks.{name}.exec must be a list of args")
                if "tcp" in h:
                    try:
                        port = int((h.get("tcp") or {}).get("port", -1))
                        if port <= 0 or port > 65535:
                            warnings.append(f"rollout.hooks.{name}.tcp.port must be 1-65535")
                    except Exception:
                        warnings.append(f"rollout.hooks.{name}.tcp.port must be an integer")
    except Exception:
        pass
    # Ingress/TLS validation: warn when tlsSecretName is set but cannot be resolved to PEMs
    ing = getattr(manifest.spec, "ingress", None)
    if ing and getattr(ing, "tls", True) and getattr(ing, "tls_secret_name", None):
        try:
            from ae.ingress.tls_sync import TlsSecretResolver

            root = _os.getenv("AE_TLS_DIR", "state/tls")
            res = TlsSecretResolver(Path(root)).resolve(str(ing.tls_secret_name))
            if res is None:
                warnings.append(
                    f"ingress.tlsSecretName '{ing.tls_secret_name}' not found under AE_TLS_DIR={root}; controller will fall back to Caddy 'tls internal'"
                )
                diagnostics["tls"] = {
                    "ingress": True,
                    "secretName": str(ing.tls_secret_name),
                    "root": root,
                    "resolved": False,
                }
            else:
                diagnostics["tls"] = {
                    "ingress": True,
                    "secretName": str(ing.tls_secret_name),
                    "root": root,
                    "resolved": True,
                    "cert": str(res[0]),
                    "key": str(res[1]),
                }
        except Exception:
            warnings.append(
                "failed to validate ingress.tlsSecretName; check AE_TLS_DIR and secret files"
            )
            diagnostics["tls"] = {
                "ingress": True,
                "secretName": str(getattr(ing, "tls_secret_name", "")),
                "error": True,
            }
    vols = getattr(manifest.spec, "volumes", []) or []
    for v in vols:
        if not getattr(v, "read_only", True):
            warnings.append(
                f"hostPath bind at {getattr(v, 'mount_path', '')} is RW; consider spec.storage PV-lite for persistence"
            )
    # Recommend readiness probe
    health = getattr(manifest.spec, "health", None)
    if not health or not getattr(health, "readiness", None):
        warnings.append(
            "no readiness probe configured; add HTTP or TCP probe for smoother rollouts"
        )
    # Recommend security hardening
    sec = getattr(manifest.spec, "security", None)
    if not sec:
        warnings.append(
            "no security spec; consider runAsUser/runAsGroup and readOnlyRootFilesystem"
        )
    else:
        if not getattr(sec, "run_as_user", None):
            warnings.append("security.runAsUser not set; prefer non-root UID (e.g., 1000)")
        if not getattr(sec, "read_only_root", False):
            warnings.append("security.readOnlyRootFilesystem is false; enable if possible")
    # Guidance for multi-replica without ingress
    if desired > 1 and not getattr(manifest.spec, "ingress", None):
        port_list = list(getattr(manifest.spec, "ports", []) or [])
        if port_list:
            warnings.append(
                "multi-replica app without ingress: per-replica host ports will be ephemeral; use ingress for HTTP access"
            )
            http_names = {"http", "https", "web"}
            non_http = [
                p for p in port_list if str(getattr(p, "name", "")).lower() not in http_names
            ]
            if non_http:
                warnings.append(
                    "multi-replica non-HTTP (L4) detected; consider an external TCP proxy (HAProxy/Traefik TCP) — see docs/l4-services.md"
                )
    if getattr(args, "json", False):
        import json as _json

        out = {
            "app": manifest.metadata.name,
            "replicas": desired,
            "rollout": {
                "strategy": strategy,
                "maxSurge": rollout.get("maxSurge", 1),
                "maxUnavailable": rollout.get("maxUnavailable", 0),
            },
            "service": (
                {
                    "port": getattr(svc, "port", None),
                    "targetPort": getattr(svc, "target_port", None),
                }
                if svc
                else None
            ),
            "warnings": warnings,
            "diagnostics": diagnostics,
            "conflicts": [],
            "ok": not warnings or not getattr(args, "strict", False),
        }
        print(_json.dumps(out, indent=2))
        return 0 if out["ok"] else 3
    else:
        for w in warnings:
            print(f"  ! warning: {w}")
        if getattr(args, "strict", False) and warnings:
            print("  ! Planner strict mode: warnings treated as errors")
            return 3
        print("  - actions: create or reconcile containers; update ingress as needed")
        print("Plan OK.")
        return 0


def handle_export_k8s(args: argparse.Namespace) -> int:
    from ae.controller.spec import load_manifest

    try:
        man = load_manifest(args.file)
    except Exception as exc:  # noqa: BLE001
        print(f"failed to load manifest: {exc}")
        return 1
    # guard mutually exclusive PDB options
    if (
        getattr(args, "pdb_min_available", None) is not None
        and getattr(args, "pdb_max_unavailable", None) is not None
    ):
        print("error: --pdb-min-available and --pdb-max-unavailable are mutually exclusive")
        return 2
    opts = ExportOptions(
        workload_kind=str(getattr(args, "workload", "deployment")).title(),
        namespace=str(args.namespace or "default"),
        ingress_class_name=args.ingress_class,
        service_port=args.service_port,
        emit_configs=bool(getattr(args, "emit_configs", False)),
        inline_configs=bool(getattr(args, "inline_configs", False)),
        emit_secrets=bool(getattr(args, "emit_secrets", False)),
        inline_secrets=bool(getattr(args, "inline_secrets", False)),
        emit_storage=bool(getattr(args, "emit_storage", False)),
        default_pvc_size=str(getattr(args, "default_pvc_size", "1Gi")),
        storage_class_name=getattr(args, "storage_class_name", None),
        service_account_name=getattr(args, "service_account", None),
        emit_pdb=bool(getattr(args, "emit_pdb", False)),
        pdb_min_available=getattr(args, "pdb_min_available", None),
        pdb_max_unavailable=getattr(args, "pdb_max_unavailable", None),
        hpa_min=getattr(args, "hpa_min", None),
        hpa_max=getattr(args, "hpa_max", None),
        hpa_cpu_target=getattr(args, "hpa_cpu_target", None),
        hpa_mem_target=getattr(args, "hpa_mem_target", None),
        hpa_mem_type=getattr(args, "hpa_mem_type", None),
        hpa_mem_value=getattr(args, "hpa_mem_value", None),
        allow_hpa_without_requests=bool(getattr(args, "allow_hpa_no_requests", False)),
        default_security=bool(getattr(args, "default_security", False)),
        require_requests=bool(getattr(args, "require_requests", False)),
        hpa_behavior_up=(__import__("json").loads(args.hpa_behavior_up) if getattr(args, "hpa_behavior_up", None) else None),
        hpa_behavior_down=(__import__("json").loads(args.hpa_behavior_down) if getattr(args, "hpa_behavior_down", None) else None),
        emit_network_policy=bool(getattr(args, "emit_np", False)),
        np_default_deny_ingress=bool(getattr(args, "np_deny_ingress", False)),
        np_default_deny_egress=bool(getattr(args, "np_deny_egress", False)),
        np_allow_dns=bool(getattr(args, "np_allow_dns", False)),
        np_allow_web=bool(getattr(args, "np_allow_web", False)),
        np_allow_internal_ports=(
            [
                int(p)
                for p in (getattr(args, "np_allow_internal_port", []) or [])
                if str(p).strip().isdigit()
            ]
            or ([5432, 6379, 3306] if getattr(args, "np_preset", None) == "backend" else [])
        ),
        inject_topology_spread=bool(getattr(args, "spread_by_host", False)),
    )
    # Apply preset last so explicit flags take precedence
    if getattr(args, "preset", None):
        opts = apply_preset(opts, args.preset)  # type: ignore[arg-type]
    out = export_k8s_yaml(manifest=man, options=opts)
    if getattr(args, "validate", False):
        ok, errs = validate_documents(out)
        if not ok:
            print("validation failed:")
            for e in errs:
                print(f"  - {e}")
            return 2
    # Split output into individual files when requested
    if getattr(args, "split", None):
        from ae.k8s.exporter import export_k8s_docs
        import yaml as _yaml
        outdir: Path = args.split
        outdir.mkdir(parents=True, exist_ok=True)
        docs = export_k8s_docs(man, options=opts)
        for i, d in enumerate(docs, start=1):
            meta = d.get("metadata") or {}
            name = (meta.get("name") or f"res-{i}")
            kind = (d.get("kind") or "Resource").lower()
            fn = outdir / f"{i:02d}-{kind}-{name}.yaml"
            fn.write_text(_yaml.safe_dump(d, sort_keys=False), encoding="utf-8")
        return 0

    output_target = args.output or getattr(args, "out", None)
    if output_target:
        try:
            Path(output_target).write_text(out, encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            print(f"failed to write output: {exc}")
            return 1
    else:
        print(out, end="")
    return 0


def handle_k8s_check(args: argparse.Namespace) -> int:
    from ae.controller.spec import load_manifest

    try:
        man = load_manifest(args.file)
    except Exception as exc:  # noqa: BLE001
        print(f"failed to load manifest: {exc}")
        return 1
    from ae.k8s.check import apply_policy

    issues = k8s_portability_issues(man)
    policy = getattr(args, "policy", "baseline")
    if getattr(args, "strict", False):
        policy = "strict"
    # Extra HPA validations based on assumptions
    if getattr(args, "assume_hpa", None):
        from ae.k8s.check import infer_hpa_issues

        issues.extend(infer_hpa_issues(man, list(getattr(args, "assume_hpa", []))))
    issues = apply_policy(issues, policy)
    if getattr(args, "json", False):
        import json as _json

        print(
            _json.dumps(
                {
                    "app": man.metadata.name,
                    "issues": [issue.__dict__ for issue in issues],
                    "ok": not issues,
                },
                indent=2,
            )
        )
        return (
            0
            if not issues
            else (2 if any(i.level == "error" for i in issues) or policy == "strict" else 0)
        )
    # text output
    if not issues:
        print("All checks passed.")
        rc = 0
    else:
        for it in issues:
            tag = "ERROR" if it.level == "error" else "WARN "
            print(f"[{tag}] {it.code}: {it.message}")
        has_err = any(i.level == "error" for i in issues)
        has_warn = any(i.level == "warn" for i in issues)
        if has_err:
            rc = 2
        elif bool(getattr(args, "fail_on_warn", False)) and has_warn:
            rc = 2
        else:
            rc = 3 if policy == "strict" else 0

    # Optional: emit exported YAML and run kubeconform
    try:
        if bool(getattr(args, "emit", False)) or bool(getattr(args, "kubeconform", False)):
            # Build export with default options for validation
            from ae.k8s.exporter import export_k8s_yaml, ExportOptions

            yaml_text = export_k8s_yaml(man, options=ExportOptions(namespace="default"))
            if bool(getattr(args, "emit", False)):
                print("---\n# Exported YAML used for validation:")
                print(yaml_text, end="")
            if bool(getattr(args, "kubeconform", False)):
                import shutil
                import subprocess as sp

                bin_path = getattr(args, "kubeconform_bin", "kubeconform")
                if shutil.which(bin_path) is None:
                    print("(kubeconform not found; skipping schema validation)")
                else:
                    cmd = [bin_path, "-strict", "-summary"]
                    if getattr(args, "kube_version", None):
                        cmd += ["-kubernetes-version", str(args.kube_version)]
                    proc = sp.run(cmd, input=yaml_text.encode("utf-8"), capture_output=True)
                    out = (proc.stdout or b"").decode()
                    err = (proc.stderr or b"").decode()
                    if out.strip():
                        print(out.strip())
                    if err.strip():
                        print(err.strip())
                    if proc.returncode != 0 and rc == 0:
                        rc = 2
    except Exception:
        pass
    # Optional summary footer
    if bool(getattr(args, "summary", False)):
        total = len(issues)
        errs = len([i for i in issues if i.level == "error"])
        warns = len([i for i in issues if i.level == "warn"])
        print(f"summary: {total} issues ({errs} errors, {warns} warnings). exit={rc}")
        print("legend: 0=ok, 2=failure (errors or fail-on-warn), 3=strict warns")
    return rc


def handle_verify_image(args: argparse.Namespace) -> int:
    """Verify container image signatures using cosign.

    Supports key-based and keyless verification. Prints a one-line summary
    and returns an appropriate exit code. With --json, emits { ok, image, summary }.
    """
    import shutil
    import subprocess as sp
    import json as _json

    cosign_bin = getattr(args, "cosign_bin", "cosign")
    cosign_path = shutil.which(cosign_bin)
    if cosign_path is None:
        msg = "cosign binary not found; install cosign or pass --cosign-bin"
        if getattr(args, "json", False):
            print(_json.dumps({"ok": False, "image": args.image, "summary": msg}))
        else:
            print(f"error: {msg}")
        return 127

    cmd = [cosign_path, "verify"]
    if getattr(args, "key", None):
        cmd += ["--key", str(args.key)]
    if getattr(args, "certificate_identity", None):
        cmd += ["--certificate-identity", str(args.certificate_identity)]
    if getattr(args, "certificate_oidc_issuer", None):
        cmd += ["--certificate-oidc-issuer", str(args.certificate_oidc_issuer)]
    if getattr(args, "attachment", None):
        cmd += ["--attachment", str(args.attachment)]
    if getattr(args, "rekor_url", None):
        cmd += ["--rekor-url", str(args.rekor_url)]
    cmd += [str(args.image)]

    try:
        proc = sp.run(cmd, capture_output=True, text=True, check=False)
    except Exception as exc:  # noqa: BLE001
        summary = f"cosign failed to start: {exc}"
        if getattr(args, "json", False):
            print(_json.dumps({"ok": False, "image": args.image, "summary": summary}))
        else:
            print(f"error: {summary}")
        return 1

    ok = proc.returncode == 0
    summary = (
        (proc.stdout or proc.stderr or "").strip().splitlines()[-1]
        if (proc.stdout or proc.stderr)
        else ("verify: ok" if ok else "verify: failed")
    )
    if getattr(args, "json", False):
        print(_json.dumps({"ok": ok, "image": args.image, "summary": summary}))
    else:
        print(summary)
    return 0 if ok else (proc.returncode or 1)


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
    # Requests strictness: warn when requests are missing
    res = getattr(manifest.spec, "resources", None)
    req = getattr(res, "requests", None) if res else None
    if not (req and getattr(req, "cpu", None)):
        warnings.append(
            "no resources.requests.cpu; set at least 100m for portability and HPA readiness"
        )
    if not (req and getattr(req, "memory", None)):
        warnings.append(
            "no resources.requests.memory; set a baseline (e.g., 128Mi) for scheduling consistency"
        )
