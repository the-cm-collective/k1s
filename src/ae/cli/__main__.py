# ruff: noqa: E501,S603,S607,S110,S112,SIM102,SIM105,SIM114,SIM210,C401,C414,S104,S105,UP017,UP038
"""Command-line interface for the ae orchestrator."""

from __future__ import annotations

import argparse
import logging
import os
import shutil
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

from ae import __version__ as AE_VERSION
from ae import build_info as AE_BUILD_INFO
from ae.config.manager import ConfigManager
from ae.controller.health import HealthManager
from ae.controller.reconciler import Reconciler, ReconcileReport
from ae.controller.spec import (
    AppManifest,
    app_key,
    app_key_for_manifest,
    format_app_ref,
    normalize_namespace,
    parse_app_ref,
    split_app_key,
)
from ae.controller.state import AppStatus, SQLiteStateStore, state_store_from_env
from ae.ingress import CaddyIngressManager, IngressService
from ae.k8s.check import k8s_portability_issues
from ae.k8s.exporter import ExportOptions, export_k8s_yaml
from ae.k8s.presets import apply_preset
from ae.k8s.validate import validate_documents
from ae.observability import MetricsService
from ae.observability.logging import configure_logging
from ae.runtime import (
    CRIRuntime,
    DockerRuntime,
    PodmanRuntime,
    RegistryAuthProvider,
    RuntimeAdapter,
    StubRuntime,
)
from ae.secrets import SecretManager


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ae", description="Minimal workload engine CLI (Deployment manifests)"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    parser.add_argument("--verbose", action="store_true", help="Enable DEBUG logging")
    parser.add_argument(
        "--log-level", default=None, help="Override log level (DEBUG/INFO/WARNING/ERROR)"
    )
    parser.add_argument(
        "--server",
        default=os.getenv("AE_API_SERVER") or None,
        help="Remote API base URL (e.g. http://127.0.0.1:9108)",
    )
    parser.add_argument("--token", default=None, help="Bearer token for remote API auth")
    parser.add_argument(
        "-n",
        "--namespace",
        default=os.getenv("AE_NAMESPACE"),
        help="Default namespace for app commands when the name is unqualified (also AE_NAMESPACE)",
    )

    def _add_namespace_arg(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "-n",
            "--namespace",
            default=argparse.SUPPRESS,
            help="Namespace to operate in when the app name is unqualified (overrides global --namespace)",
        )

    # work queue helpers (lab-edge / outbox)
    work_parser = subparsers.add_parser("work", help="Work queue helpers")
    work_sub = work_parser.add_subparsers(dest="work_cmd", required=True)
    work_enqueue = work_sub.add_parser("enqueue", help="Enqueue work for a site")
    work_enqueue.add_argument("--site-id", required=True, help="Target site id")
    work_enqueue.add_argument("--work-id", default=None, help="Work id (defaults to uuid)")
    work_enqueue.add_argument("--attempt", type=int, default=1, help="Attempt number")
    work_enqueue.add_argument(
        "--mode",
        choices=["outbox", "queue"],
        default="outbox",
        help="outbox publishes via JetStream; queue is lab-edge work.pull",
    )
    work_enqueue.add_argument("--payload", default=None, help="JSON payload override")
    work_enqueue.add_argument(
        "--payload-file", type=Path, default=None, help="JSON payload file"
    )
    work_enqueue.add_argument("--op", default=None, help="Operation name (optional)")
    work_enqueue.add_argument("--preferred-node", default=None, help="Preferred node id")
    work_enqueue.add_argument("--target", default=None, help="Target JSON string")

    apply_parser = subparsers.add_parser("apply", help="Apply a workload manifest (Deployment)")
    _add_namespace_arg(apply_parser)
    apply_parser.add_argument("-f", "--file", type=Path, required=True, help="Path to manifest")
    apply_parser.add_argument(
        "--k8s",
        action="store_true",
        help="Treat input as Kubernetes manifests (Deployment/Service/Ingress)",
    )
    apply_parser.add_argument(
        "--force-namespace",
        action="store_true",
        help="Override metadata.namespace in the manifest(s) with --namespace",
    )

    status_parser = subparsers.add_parser("status", help="Show workload (Deployment) status")
    _add_namespace_arg(status_parser)
    status_parser.add_argument("name", nargs="?", help="Workload name (omit to list all)")
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
    status_parser.add_argument(
        "--watch",
        type=int,
        default=None,
        help="Poll every N seconds until desired replicas are ready (returns nonzero on timeout)",
    )
    status_parser.add_argument(
        "--timeout",
        type=int,
        default=None,
        help="Maximum seconds to watch before exiting nonzero (requires --watch)",
    )

    logs_parser = subparsers.add_parser("logs", help="Tail workload logs")
    _add_namespace_arg(logs_parser)
    logs_parser.add_argument("name", help="Workload name")
    logs_parser.add_argument("--follow", action="store_true", help="Stream logs continuously")
    logs_parser.add_argument(
        "--container", help="Pod selector: index (e.g. 0) or pod name", default=None
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
    _add_namespace_arg(exec_parser)
    exec_parser.add_argument("name", help="Workload name")
    exec_parser.add_argument(
        "--container", required=False, help="Target container name or pod name"
    )
    exec_parser.add_argument(
        "-i", "--stdin", action="store_true", help="Pass stdin to the container"
    )
    exec_parser.add_argument("-t", "--tty", action="store_true", help="Allocate a TTY")
    exec_parser.add_argument("--timeout", type=int, default=None, help="Timeout seconds")
    exec_parser.add_argument(
        "--apishim",
        default=None,
        help="API shim base URL for SPDY exec (defaults to AE_APISHIM_SERVER when set)",
    )
    exec_parser.add_argument(
        "--ws-fallback",
        action="store_true",
        help="Allow WebSocket exec if SPDY upgrade fails",
    )
    exec_parser.add_argument("cmd", nargs="*", help="Command to execute after --")

    shell_parser = subparsers.add_parser("shell", help="Open an interactive shell in a container")
    _add_namespace_arg(shell_parser)
    shell_parser.add_argument("name", help="Workload name")
    shell_parser.add_argument(
        "--container", required=False, help="Target container name or pod name"
    )
    shell_parser.add_argument(
        "--apishim",
        default=None,
        help="API shim base URL for SPDY exec (defaults to AE_APISHIM_SERVER when set)",
    )
    shell_parser.add_argument(
        "--ws-fallback",
        action="store_true",
        help="Allow WebSocket exec if SPDY upgrade fails",
    )
    tty_group = shell_parser.add_mutually_exclusive_group()
    tty_group.add_argument("--tty", action="store_true", help="Force TTY on")
    tty_group.add_argument("--no-tty", action="store_true", help="Disable TTY")
    shell_parser.add_argument("cmd", nargs="*", help="Shell command after --")

    pf_parser = subparsers.add_parser(
        "port-forward",
        help="Forward a local TCP port to a pod via the API shim (WebSocket)",
    )
    _add_namespace_arg(pf_parser)
    pf_parser.add_argument("name", help="Workload name")
    pf_parser.add_argument(
        "mapping",
        help="Port mapping in the form local:remote (e.g., 18080:8080).",
    )
    pf_parser.add_argument(
        "--pod",
        dest="pod",
        default=None,
        help="Pod name override (defaults to a ready pod for the app).",
    )
    pf_parser.add_argument(
        "--bind",
        default="127.0.0.1",
        help="Local bind address (default: 127.0.0.1).",
    )
    pf_parser.add_argument(
        "--apishim",
        default=None,
        help="API shim base URL for WebSocket port-forward (defaults to AE_APISHIM_SERVER when set)",
    )

    nodes_parser = subparsers.add_parser("nodes", help="List or describe nodes")
    nodes_parser.add_argument("name", nargs="?", help="Node id to describe (omit to list)")
    nodes_parser.add_argument("--cordon", action="store_true", help="Mark node unschedulable")
    nodes_parser.add_argument("--uncordon", action="store_true", help="Clear cordon on node")
    nodes_parser.add_argument(
        "--drain",
        action="store_true",
        help="Cordon node and evict workloads best-effort via the node agent",
    )

    certs_parser = subparsers.add_parser("certs", help="List issued/revoked agent certs")
    certs_parser.add_argument(
        "--root", default=None, help="TLS dir (default AE_TLS_DIR or state/tls)"
    )
    certs_parser.add_argument("--json", action="store_true", help="Emit JSON")

    rollback_parser = subparsers.add_parser("rollback", help="Rollback a workload revision")
    _add_namespace_arg(rollback_parser)
    rollback_parser.add_argument("name", help="Workload name")
    rollback_parser.add_argument(
        "--to",
        type=int,
        default=None,
        help="Target revision number (default: previous revision)",
    )

    revisions_parser = subparsers.add_parser("revisions", help="List stored revisions")
    _add_namespace_arg(revisions_parser)
    revisions_parser.add_argument("name", help="Workload name")
    revisions_parser.add_argument("--limit", type=int, default=10)

    # registry helpers
    registry_parser = subparsers.add_parser("registry", help="Manage registry credentials")
    reg_sub = registry_parser.add_subparsers(dest="registry_cmd", required=True)
    reg_sub.add_parser("list", help="List configured registries")
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
        "--host",
        action="append",
        default=[],
        help="Restrict to specific registry host(s); repeatable",
    )
    reg_secret.add_argument("--output", "-o", default="-", help="Output file ('-' for stdout)")

    metrics_parser = subparsers.add_parser("metrics", help="Show aggregated metrics")
    metrics_parser.add_argument("--json", action="store_true", help="Emit JSON output")

    events_parser = subparsers.add_parser("events", help="Show recent events")
    _add_namespace_arg(events_parser)
    events_parser.add_argument("name", help="Workload name")
    events_parser.add_argument("--limit", type=int, default=20)

    services_parser = subparsers.add_parser(
        "services", help="List Services (cluster IPs/endpoints)"
    )
    _add_namespace_arg(services_parser)
    services_parser.add_argument("--json", action="store_true", help="Emit JSON output")

    history_parser = subparsers.add_parser("history", help="Show recent probe evaluations")
    _add_namespace_arg(history_parser)
    history_parser.add_argument("name", help="Workload name")
    history_parser.add_argument("--limit", type=int, default=20)
    history_parser.add_argument("--pod", dest="pod", default=None, help="Filter by pod name")
    history_parser.add_argument(
        "--replica",
        dest="pod",
        default=None,
        help="Deprecated alias for --pod (filter by pod name)",
    )
    history_parser.add_argument("--json", action="store_true", help="Emit JSON output")
    history_parser.add_argument(
        "--since", default=None, help="Show entries since a relative duration (e.g., 10m, 2h)"
    )
    history_parser.add_argument(
        "--since-time",
        dest="since_time",
        default=None,
        help="Show entries since an RFC3339 timestamp (e.g., 2025-11-10T12:34:00Z)",
    )

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
        "delete", help="Delete a workload/app (containers + status)"
    )
    _add_namespace_arg(delete_parser)
    delete_parser.add_argument("name", help="Workload name")
    delete_parser.add_argument(
        "--purge", action="store_true", help="Also purge events and revisions history"
    )

    # scale <name> --replicas N
    scale_parser = subparsers.add_parser(
        "scale", help="Scale a workload/app by reconciling replicas"
    )
    _add_namespace_arg(scale_parser)
    scale_parser.add_argument("name", help="Workload name")
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
    plan.add_argument("--verbose", action="store_true", help="Show pod placement details")
    plan.add_argument("--strict", action="store_true", help="Treat warnings as errors")
    plan.add_argument("--json", action="store_true", help="Emit JSON instead of text")

    # export-k8s (render Kubernetes YAML)
    xk = subparsers.add_parser("export-k8s", help="Export a manifest to Kubernetes YAML")
    xk.add_argument("-f", "--file", type=Path, required=True)
    xk.add_argument(
        "--namespace",
        default=None,
        help="K8s namespace (default: manifest metadata.namespace or default)",
    )
    xk.add_argument(
        "--ingress-class", default=None, help="Ingress class name (e.g., traefik or nginx)"
    )
    xk.add_argument(
        "--ingress-path-type",
        choices=["Prefix", "Exact", "ImplementationSpecific"],
        default=None,
        help="Ingress pathType to use for all paths (default: Prefix)",
    )
    xk.add_argument(
        "--ingress-annotation",
        action="append",
        default=[],
        dest="ingress_annotation",
        help="Ingress annotation key=value (repeatable)",
    )
    xk.add_argument(
        "--ingress-preset",
        choices=["nginx-web", "traefik-web"],
        default=None,
        help="Apply curated annotations for common ingress controllers",
    )
    xk.add_argument(
        "--service-port", type=int, default=None, help="Override Service port (default: 80)"
    )
    xk.add_argument(
        "--workload",
        choices=["deployment", "statefulset", "job", "cronjob"],
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
    # Job/CronJob options
    xk.add_argument("--job-backoff-limit", type=int, default=None)
    xk.add_argument("--job-ttl-seconds-after-finished", type=int, default=None)
    xk.add_argument(
        "--cron-schedule",
        default=None,
        help="Cron expression for CronJob (required for --workload cronjob)",
    )
    xk.add_argument(
        "--cron-concurrency-policy",
        choices=["Allow", "Forbid", "Replace"],
        default=None,
        help="CronJob concurrencyPolicy (default: cluster default)",
    )
    xk.add_argument("--cron-suspend", action="store_true", help="Create CronJob in suspended state")
    xk.add_argument("--cron-starting-deadline-seconds", type=int, default=None)
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
        "--pvc-access-modes",
        action="append",
        default=None,
        help="Override PVC accessModes (repeat to provide multiple, e.g., ReadWriteOnce)",
    )
    xk.add_argument(
        "--service-account", default=None, help="Attach ServiceAccount and emit it by this name"
    )
    xk.add_argument(
        "--emit-pdb", action="store_true", help="Emit a PodDisruptionBudget when replicas > 1"
    )
    xk.add_argument(
        "--pdb-min-available",
        type=str,
        default=None,
        help="PDB minAvailable (int or percent, e.g., 1 or 50%)",
    )
    xk.add_argument(
        "--pdb-max-unavailable",
        type=str,
        default=None,
        help="PDB maxUnavailable (int or percent; mutually exclusive with minAvailable)",
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
    xk.add_argument(
        "--emit-np", action="store_true", help="Emit a default NetworkPolicy (deny-all per type)"
    )
    xk.add_argument("--np-deny-ingress", action="store_true", help="Default deny ingress")
    xk.add_argument("--np-deny-egress", action="store_true", help="Default deny egress")
    xk.add_argument(
        "--np-allow-dns",
        action="store_true",
        help="Allow DNS egress (TCP/UDP 53) when denying egress",
    )
    xk.add_argument(
        "--np-allow-web",
        action="store_true",
        help="Allow HTTP/HTTPS egress (TCP 80/443) when denying egress",
    )
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
    # Namespace + PodSecurity labels
    xk.add_argument(
        "--emit-namespace",
        action="store_true",
        help="Emit a Namespace object for the resolved namespace",
    )
    xk.add_argument(
        "--psa-enforce",
        choices=["baseline", "restricted"],
        default=None,
        help="Set pod-security.kubernetes.io/enforce label on emitted Namespace",
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
    kc.add_argument(
        "--kubeconform", action="store_true", help="Run kubeconform on exported YAML (if available)"
    )
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
        help="List of Deployment manifests to score (files)",
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
    r_pause = rollout_sub.add_parser("pause", help="Pause rollout for a workload/app")
    _add_namespace_arg(r_pause)
    r_pause.add_argument("name", help="Workload name")
    r_resume = rollout_sub.add_parser("resume", help="Resume rollout for a workload/app")
    _add_namespace_arg(r_resume)
    r_resume.add_argument("name", help="Workload name")

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

    # auth helpers
    auth_cmd = subparsers.add_parser("auth", help="Auth helpers for local/remote setup")
    auth_sub = auth_cmd.add_subparsers(dest="auth_cmd", required=True)
    auth_local = auth_sub.add_parser(
        "local", help="Emit export lines from local env files for CLI use"
    )
    auth_local.add_argument(
        "--output",
        "-o",
        type=Path,
        default=None,
        help="Write exports to this file instead of stdout",
    )
    auth_local.add_argument(
        "--apishim-env",
        type=Path,
        default=None,
        help="Path to apishim env file (default: state/profiles/labs/apishim.env)",
    )
    auth_local.add_argument(
        "--controller-env",
        type=Path,
        default=None,
        help="Path to controller env file (default: state/env.sh)",
    )
    auth_local.add_argument(
        "--dev-env",
        type=Path,
        default=None,
        help="Path to dev env file (default: state/dev.env)",
    )
    auth_local.add_argument(
        "--server",
        default=None,
        help="Override AE_APISHIM_SERVER value in the output",
    )
    auth_local.add_argument(
        "--apishim-pid",
        type=Path,
        default=None,
        help="Path to apishim pid file (default: state/apishim.pid)",
    )
    auth_remote = auth_sub.add_parser("remote", help="Generate fresh tokens for remote setup")
    auth_remote.add_argument(
        "--output",
        "-o",
        type=Path,
        default=None,
        help="Write exports to this file instead of stdout",
    )
    auth_remote.add_argument(
        "--no-mutations",
        action="store_true",
        help="Do not include AE_API_MUTATIONS=1",
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

    tls_k8s = tls_sub.add_parser(
        "kubesecret", help="Generate a kubernetes.io/tls Secret YAML from PEMs"
    )
    tls_k8s.add_argument(
        "--name", "-n", required=True, help="Secret name (also the TLS material name under root)"
    )
    tls_k8s.add_argument(
        "--namespace", default="default", help="Kubernetes namespace for the Secret"
    )
    tls_k8s.add_argument(
        "--root",
        type=Path,
        default=None,
        help="TLS root directory (defaults AE_TLS_DIR or state/tls)",
    )
    tls_k8s.add_argument(
        "--output", "-o", type=Path, default=None, help="Write Secret YAML to this file"
    )

    # volumes list
    vols = subparsers.add_parser("volumes", help="Inspect storage volumes")
    vols_sub = vols.add_subparsers(dest="vol_cmd", required=True)
    vols_list = vols_sub.add_parser("list", help="List storage volumes (PV-lite)")
    _add_namespace_arg(vols_list)
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


def runtime_factory(registry_auth: RegistryAuthProvider | None = None) -> RuntimeAdapter:
    # Default to OCI/Podman; fall back to Docker when unavailable
    backend = os.getenv("AE_RUNTIME_BACKEND", "podman").lower()
    if backend == "stub":
        return StubRuntime()
    if backend in {"cri", "containerd"}:
        return CRIRuntime(registry_auth=registry_auth)
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
            "metadata": {
                "name": getattr(args, "name", "regcred"),
                "namespace": getattr(args, "namespace", "default"),
            },
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
        provider = str(args.provider)
        reg = args.registry
        user = args.username
        pw = args.password
        token = args.token
        # Provider-specific defaults
        if provider == "ghcr":
            host = reg or "ghcr.io"
            u = user or (os.getenv("GHCR_USERNAME") or os.getenv("USER") or "")
            # Try explicit password/token, then env, then gh CLI
            p = pw or token or os.getenv("GHCR_TOKEN") or os.getenv("GH_TOKEN") or ""
            if (not p) and shutil.which("gh") is not None:
                try:
                    import subprocess as sp

                    cp = sp.run(["gh", "auth", "token"], capture_output=True, text=True, check=True)  # noqa: S603,S607 - fixed gh binary; shell disabled
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
                    )  # noqa: S603,S607 - aws CLI; shell disabled; region controlled
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
                    )  # noqa: S603,S607 - gcloud CLI; shell disabled
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
                                ["gh", "auth", "token"],
                                capture_output=True,
                                text=True,
                                check=True,
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
                            )  # noqa: S603,S607 - gcloud CLI; shell disabled
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
                            )  # noqa: S603,S607 - aws CLI; shell disabled; region controlled
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
    if os.getenv("AE_DISABLE_INGRESS") == "1":
        logging.getLogger(__name__).info("Ingress disabled via AE_DISABLE_INGRESS=1")
        return None
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
        f"Applied {_display_app_name(report.app_name)}: +{report.created}/~{report.updated}/-{report.removed}, "
        f"ready={report.ready_replicas}, live={report.live_replicas}, "
        f"rev={report.revision}({report.revision_status})"
    )


def format_status(status: AppStatus) -> str:
    parts = [
        f"{_display_app_name(status.app_name)}: desired={status.desired_replicas}",
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

    ctx: dict[str, object] = {}

    def _ensure_context() -> dict[str, object]:
        if ctx:
            return ctx
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
        ctx.update(
            {
                "store": store,
                "runtime": runtime,
                "reconciler": reconciler,
                "ingress_service": ingress_service,
            }
        )
        return ctx

    class _Lazy:
        def __init__(self, getter):
            self._getter = getter

        def _value(self):
            return self._getter()

        def __getattr__(self, name: str):
            return getattr(self._value(), name)

    store = _Lazy(lambda: _ensure_context()["store"])
    runtime = _Lazy(lambda: _ensure_context()["runtime"])
    reconciler = _Lazy(lambda: _ensure_context()["reconciler"])
    ingress_service = _Lazy(lambda: _ensure_context()["ingress_service"])

    _auth_impl = globals().get("handle_auth")
    if _auth_impl is None:

        def auth_handler(_ns: argparse.Namespace) -> int:
            print("auth command unavailable in this build")
            return 2
    else:
        auth_handler = lambda ns: _auth_impl(ns, args)

    command_handlers: dict[str, Callable[[argparse.Namespace], int]] = {
        "apply": lambda ns: handle_apply(ns, reconciler, args),
        "status": lambda ns: handle_status(ns, store, args, runtime),
        "logs": lambda ns: handle_logs(ns, store, runtime),
        "exec": lambda ns: handle_exec(ns, store, runtime),
        "shell": lambda ns: handle_shell(ns, store, runtime),
        "port-forward": lambda ns: handle_port_forward(ns, store, runtime),
        "rollback": lambda ns: handle_rollback(ns, store, reconciler),
        "revisions": lambda ns: handle_revisions(ns, store),
        "rollout": lambda ns: handle_rollout(ns, store, reconciler),
        "api": handle_api,
        "auth": auth_handler,
        "tls": handle_tls,
        "registry": handle_registry,
        "metrics": lambda ns: handle_metrics(ns, store),
        "events": lambda ns: handle_events(ns, store, args),
        "history": lambda ns: handle_history(ns, store, args),
        "services": lambda ns: handle_services(ns, store),
        "delete": lambda ns: handle_delete(ns, store, runtime, ingress_service, args),
        "scale": lambda ns: handle_scale(ns, store, reconciler, args),
        "backup": lambda ns: handle_backup(ns),
        "version": lambda _ns: handle_version(),
        "config": lambda _ns: handle_config(_ns),
        "secret": lambda _ns: handle_secret(_ns),
        "volumes": lambda ns: handle_volumes(ns, runtime),
        "examples": handle_examples,
        "plan": lambda ns: handle_plan(ns, runtime),
        "export-k8s": handle_export_k8s,
        "k8s-check": handle_k8s_check,
        "verify-image": handle_verify_image,
        "nodes": lambda ns: handle_nodes(ns, store, runtime),
        "certs": handle_certs,
        "work": lambda ns: handle_work(ns, store),
    }

    handler = command_handlers.get(args.command)
    if handler is None:
        parser.error(f"Unhandled command: {args.command}")
        return 2
    return handler(args)


def handle_apply(
    args: argparse.Namespace, reconciler: Reconciler, global_args: argparse.Namespace | None = None
) -> int:
    import yaml as _yaml

    from ae.k8s import convert as k8s_convert

    def _load_yaml_documents(path: Path) -> list[dict]:
        docs = [d for d in _yaml.safe_load_all(path.read_text()) if d]
        if not docs:
            raise ValueError("no YAML documents found")
        return docs

    k8s_workload_kinds = {"Deployment", "StatefulSet", "DaemonSet", "Job"}
    k8s_network_kinds = {"Service", "Ingress"}

    def _k8s_kind(doc: dict) -> str:
        return str(doc.get("kind") or "")

    def _k8s_api(doc: dict) -> str:
        return str(doc.get("apiVersion") or "")

    def _k8s_meta(doc: dict) -> dict:
        meta = doc.get("metadata") or {}
        return meta if isinstance(meta, dict) else {}

    def _k8s_name(doc: dict) -> str:
        meta = _k8s_meta(doc)
        return str(meta.get("name") or "")

    def _k8s_namespace(doc: dict) -> str | None:
        meta = _k8s_meta(doc)
        ns = meta.get("namespace")
        return str(ns) if ns else None

    def _should_convert_k8s(docs: list[dict], force: bool) -> bool:
        if force:
            return True

        def _is_native(doc: dict) -> bool:
            return _k8s_api(doc) == "ae.dev/v1alpha1"

        if len(docs) == 1 and _is_native(docs[0]):
            return False
        for doc in docs:
            kind = _k8s_kind(doc)
            api = _k8s_api(doc)
            if api == "ae.dev/v1alpha1":
                continue
            if kind:
                return True
        return False

    def _convert_k8s_documents(docs: list[dict]) -> tuple[AppManifest, list[str]]:
        workloads: list[dict] = []
        services: list[dict] = []
        ingresses: list[dict] = []
        unsupported: list[str] = []
        for doc in docs:
            kind = _k8s_kind(doc)
            if kind in k8s_workload_kinds:
                workloads.append(doc)
            elif kind in k8s_network_kinds:
                if kind == "Service":
                    services.append(doc)
                elif kind == "Ingress":
                    ingresses.append(doc)
            elif kind:
                unsupported.append(kind)
        if unsupported:
            raise ValueError(f"unsupported Kubernetes kinds: {', '.join(sorted(set(unsupported)))}")
        if len(workloads) != 1:
            names = ", ".join(_k8s_name(w) or "<unnamed>" for w in workloads)
            raise ValueError(
                f"expected exactly one workload (Deployment/StatefulSet/DaemonSet/Job), got {len(workloads)}: {names}"
            )

        workload = workloads[0]
        workload_kind = _k8s_kind(workload)
        workload_name = _k8s_name(workload)
        workload_ns = _k8s_namespace(workload)
        workload_key = (workload_ns, workload_name)
        labels = k8s_convert.pod_template_labels(workload)
        ports_by_name = k8s_convert.pod_template_ports_by_name(workload)

        warnings: list[str] = []
        service_spec = None
        service_name_map: dict[tuple[str | None, str], tuple[str | None, str]] = {}
        for svc in services:
            spec = svc.get("spec") if isinstance(svc.get("spec"), dict) else {}
            selector = k8s_convert.service_selector(spec or {})
            target_key = None
            if selector and k8s_convert.selector_matches(selector, labels):
                target_key = workload_key
            else:
                target = k8s_convert.fallback_service_target(svc, selector)
                if target in {
                    workload_name,
                    k8s_convert.app_name_for_k8s(workload_ns, workload_name),
                }:
                    target_key = workload_key
            if not target_key:
                continue
            if service_spec is not None:
                warnings.append("multiple Services matched the workload; using the first one")
                continue
            service_spec = k8s_convert.service_spec_from_k8s(svc, ports_by_name)
            if service_spec is None:
                warnings.append("matched Service has no usable ports; skipping Service mapping")
                continue
            service_name_map[(_k8s_namespace(svc), _k8s_name(svc))] = target_key

        ingress_spec = None
        for ing in ingresses:
            res = k8s_convert.ingress_spec_from_k8s(ing, service_name_map)
            if not res:
                continue
            target_key, spec = res
            if target_key != workload_key:
                continue
            if ingress_spec is not None:
                warnings.append("multiple Ingresses matched the workload; using the first one")
                continue
            ingress_spec = spec

        manifest = k8s_convert.manifest_from_k8s_workload(
            workload, service_spec=service_spec, ingress_spec=ingress_spec
        )
        if workload_kind == "Job":
            spec = manifest.spec
            updates: dict[str, Any] = {"workload": "job"}
            job_spec = workload.get("spec") or {}
            try:
                if job_spec.get("backoffLimit") is not None:
                    updates["job_backoff_limit"] = int(job_spec.get("backoffLimit"))
            except Exception:
                pass
            try:
                if job_spec.get("ttlSecondsAfterFinished") is not None:
                    updates["job_ttl_seconds_after_finished"] = int(
                        job_spec.get("ttlSecondsAfterFinished")
                    )
            except Exception:
                pass
            manifest = manifest.model_copy(update={"spec": spec.model_copy(update=updates)})

        return manifest, warnings

    def _doc_has_namespace(doc: dict) -> bool:
        meta = doc.get("metadata")
        if not isinstance(meta, dict):
            return False
        ns = meta.get("namespace")
        return bool(str(ns).strip()) if ns is not None else False

    def _apply_namespace_override(
        docs: list[dict], namespace: str | None, *, force: bool = False
    ) -> None:
        if not namespace:
            return
        for doc in docs:
            if not isinstance(doc, dict):
                continue
            meta = doc.get("metadata")
            if not isinstance(meta, dict):
                meta = {}
                doc["metadata"] = meta
            ns_val = meta.get("namespace")
            if force or ns_val is None or not str(ns_val).strip():
                meta["namespace"] = namespace

    ns_override = normalize_namespace(getattr(args, "namespace", None))
    force_namespace = bool(getattr(args, "force_namespace", False))

    # Remote apply via API when --server is set
    if global_args and getattr(global_args, "server", None):
        base = str(global_args.server)
        tok = getattr(global_args, "token", None)
        try:
            docs = _load_yaml_documents(args.file)
            _apply_namespace_override(docs, ns_override, force=force_namespace)
        except Exception as exc:  # noqa: BLE001
            print(f"failed to read manifest: {exc}")
            return 1
        try:
            if _should_convert_k8s(docs, bool(getattr(args, "k8s", False))):
                manifest, warnings = _convert_k8s_documents(docs)
                for w in warnings:
                    print(f"warning: {w}")
                payload = manifest.model_dump(by_alias=True)
            else:
                if len(docs) != 1:
                    raise ValueError("expected a single Deployment manifest document")
                payload = docs[0]
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
    from ae.controller.spec import ManifestError, load_manifest

    try:
        docs = _load_yaml_documents(args.file)
        raw_has_namespace = (
            _doc_has_namespace(docs[0]) if docs and isinstance(docs[0], dict) else False
        )
        _apply_namespace_override(docs, ns_override, force=force_namespace)
        if _should_convert_k8s(docs, bool(getattr(args, "k8s", False))):
            manifest, warnings = _convert_k8s_documents(docs)
            for w in warnings:
                print(f"warning: {w}")
        else:
            if len(docs) != 1:
                raise ManifestError("expected a single Deployment manifest document")
            manifest = load_manifest(args.file)
            if ns_override and (force_namespace or not raw_has_namespace):
                manifest = manifest.model_copy(
                    update={
                        "metadata": manifest.metadata.model_copy(update={"namespace": ns_override})
                    }
                )
    except (ManifestError, ValueError) as exc:
        print(f"failed to read manifest: {exc}")
        return 1
    try:
        store = state_store_from_env()
        existing = store.get_registered_entry(app_key_for_manifest(manifest))
        src = existing.source if existing else "cli"
        lbls = existing.labels if existing else getattr(manifest.metadata, "labels", None)
        store.register_app(manifest, source=src, labels=lbls)
    except Exception:
        pass
    report = reconciler.reconcile(manifest)
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
            sp.run(
                [sops, "-e", "-o", str(ns.output), str(ns.input)],
                check=True,
            )  # noqa: S603,S607 - sops binary resolved; shell disabled
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
            sp.run(
                [sops, "-d", "-o", str(ns.output), str(ns.input)],
                check=True,
            )  # noqa: S603,S607 - sops binary resolved; shell disabled
            print(f"decrypted → {ns.output}")
            return 0
        except sp.CalledProcessError as exc:
            print(f"sops decrypt failed: {exc}")
            return 1
    print(f"Unsupported secret command: {ns.secret_cmd}")
    return 1


def _fmt_labels(labels: dict) -> str:
    if not labels:
        return "-"
    return ",".join(f"{k}={v}" for k, v in sorted(labels.items()))


def _fmt_taints(taints: list) -> str:
    if not taints:
        return "-"
    parts = []
    for t in taints:
        if isinstance(t, dict):
            key = t.get("key") or ""
            eff = t.get("effect") or ""
            val = t.get("value")
            if val:
                parts.append(f"{key}={val}:{eff}")
            else:
                parts.append(f"{key}:{eff}")
    return ",".join(parts) if parts else "-"


def _node_status_with_staleness(status, *, grace_seconds: int = 40) -> str:  # type: ignore[no-untyped-def]
    if status is None:
        return "Unknown"
    st = status.status or "Unknown"
    try:
        age = (datetime.now(UTC) - status.seen_at).total_seconds()
        if age > grace_seconds and st == "Ready":
            return f"NotReady (stale {int(age)}s)"
    except Exception:
        return st
    return st


def handle_nodes(
    ns: argparse.Namespace, store: SQLiteStateStore, runtime: RuntimeAdapter | None = None
) -> int:
    # Mutating operations: cordon/uncordon/drain
    if (
        getattr(ns, "cordon", False)
        or getattr(ns, "uncordon", False)
        or getattr(ns, "drain", False)
    ):
        if not getattr(ns, "name", None):
            print("node name is required for cordon/uncordon/drain")
            return 2
        res = store.get_node(ns.name)
        if res is None:
            print(f"node {ns.name} not found")
            return 1
        node, _status = res
        if getattr(ns, "uncordon", False):
            store.cordon_node(node.node_id, False)
            print(f"uncordoned {node.node_id}")
            return 0
        # cordon or drain
        store.cordon_node(node.node_id, True)
        if getattr(ns, "drain", False) and node.endpoint and runtime is not None:
            try:
                from ae.runtime import RemoteRuntime

                rr = RemoteRuntime(node.endpoint, runtime)
                infos = rr.list_containers_info()
                apps = {
                    (info.get("labels") or {}).get("ae.app")
                    for info in infos
                    if (info.get("labels") or {}).get("ae.app")
                }
                for app in apps:
                    try:
                        rr.remove_app(str(app))
                    except Exception:
                        pass
                print(
                    f"drained {node.node_id}: evicted {len(apps)} app(s); node cordoned for reschedule"
                )
                return 0
            except Exception as exc:  # noqa: BLE001
                print(f"cordoned {node.node_id} (drain best-effort failed: {exc})")
                return 1
        print(f"cordoned {node.node_id}")
        return 0

    if getattr(ns, "name", None):
        res = store.get_node(ns.name)
        if res is None:
            print(f"node {ns.name} not found")
            return 1
        node, status = res
        print(f"node {node.node_id}")
        print(f"  name:     {node.name or '-'}")
        print(f"  backend:  {node.backend or '-'}")
        print(f"  endpoint: {node.endpoint or '-'}")
        print(f"  podCIDR:  {node.pod_cidr or '-'}")
        print(f"  wgPubkey: {node.wg_pubkey or '-'}")
        print(f"  rpPubkey: {getattr(node, 'rp_pubkey', None) or '-'}")
        print(f"  labels:   {_fmt_labels(node.labels)}")
        print(f"  taints:   {_fmt_taints(node.taints)}")
        print(f"  cordoned: {'yes' if getattr(node, 'cordoned', False) else 'no'}")
        if status:
            ts = status.seen_at.strftime("%Y-%m-%d %H:%M:%S %Z")
            print(f"  status:   {_node_status_with_staleness(status)} (seen {ts})")
        else:
            print("  status:   <none>")
        return 0

    rows = store.list_nodes()
    if not rows:
        print("No nodes registered.")
        return 0
    for node, status in rows:
        st = _node_status_with_staleness(status)
        print(
            f"{node.node_id}: status={st} cordoned={'yes' if getattr(node, 'cordoned', False) else 'no'} backend={node.backend or '-'} endpoint={node.endpoint or '-'} labels={_fmt_labels(node.labels)}"
        )
    return 0


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
            app_name = _resolve_app_name(args.name, getattr(args, "namespace", None)) or args.name
            resp = _http_post_json(
                base, f"/delete/{app_name}?purge={'1' if args.purge else '0'}", {}, tok
            )
            print(
                f"deleted {_display_app_name(app_name)}: removed={resp.get('removed', 0)} containers{' (purged history)' if resp.get('purged') else ''}"
            )
            return 0
        except Exception as exc:  # noqa: BLE001
            print(f"remote delete failed: {exc}")
            return 1
    name = _resolve_app_name(args.name, getattr(args, "namespace", None)) or args.name
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
    try:
        store.delete_registered_app(name)
    except Exception:
        pass
    store.delete_app_state(name, purge_history=bool(args.purge))
    print(
        f"deleted {_display_app_name(name)}: removed={removed} containers{' (purged history)' if args.purge else ''}"
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
            app_name = _resolve_app_name(args.name, getattr(args, "namespace", None)) or args.name
            resp = _http_post_json(
                base, f"/scale/{app_name}", {"replicas": int(args.replicas)}, tok
            )
            print(
                f"scaled {_display_app_name(app_name)} to replicas={resp.get('replicas')} rev={resp.get('revision')}({resp.get('status')}) "
            )
            return 0
        except Exception as exc:  # noqa: BLE001
            print(f"remote scale failed: {exc}")
            return 1
    name = _resolve_app_name(args.name, getattr(args, "namespace", None)) or args.name
    latest = store.list_revisions(name, limit=1)
    if not latest:
        print(f"No revisions recorded for {_display_app_name(name)}. Try 'ae apply -f <manifest>'.")
        return 1
    manifest = store.get_revision_manifest(name, latest[0].revision)
    updated_spec = manifest.spec.model_copy(update={"replicas": int(args.replicas)})
    new_manifest = manifest.model_copy(update={"spec": updated_spec})
    try:
        existing = store.get_registered_entry(name)
        src = existing.source if existing else "cli"
        lbls = existing.labels if existing else getattr(new_manifest.metadata, "labels", None)
        store.register_app(new_manifest, source=src, labels=lbls)
    except Exception:
        pass
    report = reconciler.reconcile(new_manifest)
    print(
        f"scaled {_display_app_name(name)} to replicas={args.replicas}: rev={report.revision}({report.revision_status}) "
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
    import json
    import shutil
    import tempfile
    from datetime import datetime

    from ae.controller.spec import load_manifest

    # Build export options (with preset)
    preset = getattr(args, "np_preset", None)
    if preset == "web":
        args.emit_np = True
        args.np_deny_ingress = True
        args.np_deny_egress = True
        args.np_allow_dns = True
        args.np_allow_web = True
    elif preset == "backend":
        args.emit_np = True
        args.np_deny_ingress = True
        args.np_deny_egress = True
        args.np_allow_dns = True
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
                    )  # noqa: S603,S607 - kubeconform binary vetted by shutil.which
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
                    )  # noqa: S603,S607 - kubectl binary vetted; shell disabled
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
                )  # noqa: S603,S607 - kubectl binary vetted; shell disabled
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
                    )  # noqa: S603,S607 - kubectl binary vetted; shell disabled
                    # Try to find the Deployment name(s) from parsed docs
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
                    )  # noqa: S603,S607 - kubectl binary vetted; shell disabled
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
        "generated_at": datetime.now(UTC).isoformat(),
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


def handle_work(ns: argparse.Namespace, store: SQLiteStateStore) -> int:
    if ns.work_cmd != "enqueue":
        print("unsupported work command")
        return 2
    import json as _json
    import uuid as _uuid
    from datetime import datetime, timezone

    work_id = ns.work_id or str(_uuid.uuid4())
    attempt = int(ns.attempt or 1)
    site_id = ns.site_id
    payload = {}
    if ns.payload_file:
        payload = _json.loads(Path(ns.payload_file).read_text(encoding="utf-8"))
    elif ns.payload:
        payload = _json.loads(ns.payload)
    if not isinstance(payload, dict):
        print("payload must be a JSON object")
        return 2
    payload.setdefault("work_id", work_id)
    payload.setdefault("attempt", attempt)
    payload.setdefault("site_id", site_id)
    if ns.op:
        payload.setdefault("op", ns.op)
    if ns.preferred_node:
        payload.setdefault("preferred_node", ns.preferred_node)
    if ns.target:
        try:
            payload.setdefault("target", _json.loads(ns.target))
        except Exception as exc:  # noqa: BLE001
            print(f"invalid --target json: {exc}")
            return 2
    payload.setdefault("created_at", datetime.now(timezone.utc).isoformat())
    desired_generation = payload.get("desired_generation")
    try:
        desired_generation = int(desired_generation) if desired_generation is not None else None
    except Exception:
        desired_generation = None
    store.upsert_work_ledger(
        work_id=work_id,
        attempt=attempt,
        site_id=site_id,
        state="Pending",
        desired_generation=desired_generation,
    )
    if ns.mode == "queue":
        store.enqueue_work(work_id, attempt, site_id, payload)
        print(f"enqueued work_id={work_id} attempt={attempt} site={site_id} mode=queue")
    else:
        store.enqueue_work_outbox(work_id, attempt, site_id, payload)
        print(f"enqueued work_id={work_id} attempt={attempt} site={site_id} mode=outbox")
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


def _resolve_app_name(name: str | None, namespace: str | None = None) -> str | None:
    if not name:
        return None
    ns, base = parse_app_ref(name)
    if ns is None:
        ns = normalize_namespace(namespace)
    return app_key(base, ns)


def _display_app_name(app_name: str) -> str:
    return format_app_ref(app_name)


def handle_status(
    args: argparse.Namespace,
    store: SQLiteStateStore,
    global_args: argparse.Namespace,
    runtime: RuntimeAdapter | None = None,
) -> int:
    ns_filter = normalize_namespace(getattr(args, "namespace", None))
    if getattr(global_args, "server", None):
        base = str(global_args.server)
        tok = getattr(global_args, "token", None)
        try:
            if args.name:
                app_name = _resolve_app_name(args.name, ns_filter) or args.name
                path = f"/status/{app_name}"
                if args.wide:
                    path += "?details=1"
                data = _http_get_json(base, path, tok)
                print(
                    ", ".join(
                        [
                            f"{_display_app_name(data['app_name'])}: desired={data['desired_replicas']}",
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
                # When --wide, include pod and container details if available
                if args.wide:
                    try:
                        for r in (data.get("pods") or data.get("replicas") or []):
                            pod_name = r.get("pod_name") or r.get("replica_id")
                            print(
                                f"  - {pod_name}: ready={bool(r.get('ready'))} "
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
            items = page.get("items", []) if isinstance(page, dict) else []
            if ns_filter:
                items = [
                    s0
                    for s0 in items
                    if split_app_key(str((s0 or {}).get("app_name", "")))[0] == ns_filter
                ]
            for s0 in items:
                line = ", ".join(
                    [
                        f"{_display_app_name(s0['app_name'])}: desired={s0['desired_replicas']}",
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
        app_name = _resolve_app_name(args.name, ns_filter) or args.name
        status = store.get_status(app_name)
        if status is None:
            print(f"No status recorded for {_display_app_name(app_name)}")
            return 1

        def _print_status_block(st: AppStatus) -> None:
            if args.json:
                print(_status_to_json(st, store, include_details=args.wide))
                return
            print(format_status(st))
            if args.wide:
                try:
                    manifest = store.get_revision_manifest(app_name, st.revision)
                    res = manifest.spec.resources
                    vols = manifest.spec.volumes
                    if res and res.limits:
                        cpu = res.limits.cpu if res.limits.cpu is not None else "-"
                        mem = res.limits.memory if res.limits.memory is not None else "-"
                        print(f"    resources: limits cpu={cpu}, memory={mem}")
                    try:
                        events = store.list_events(app_name, limit=10)
                        if any(e.event_type == "CrashLoopDetected" for e in events):
                            print(
                                "    crashloop: recent CrashLoopDetected events present (see 'ae events')"
                            )
                    except Exception:
                        pass
                    if vols:
                        print(f"    volumes: {len(vols)} mounts")
                    storage = getattr(manifest.spec, "storage", []) or []
                    if storage:
                        st_str = ", ".join(f"{s.name}:{s.mount_path}" for s in storage)
                        print(f"    storage: {st_str}")
                except Exception:
                    pass
            pods = store.list_pods(app_name)
            for pod in pods:
                print(
                    f"  - {pod.pod_name}: ready={pod.ready} "
                    f"live={pod.live} status={pod.status} | "
                    f"readiness={pod.readiness_message}; "
                    f"liveness={pod.liveness_message}"
                )
            if args.history and args.history > 0:
                history = store.get_probe_history(app_name, args.history)
                for entry in history:
                    timestamp = entry.check_time.strftime("%Y-%m-%d %H:%M:%S")
                    print(
                        f"    history {timestamp} {entry.pod_name}: ready={entry.ready} "
                        f"live={entry.live} | readiness={entry.readiness_message}; "
                        f"liveness={entry.liveness_message}"
                    )
            if args.events:
                events = store.list_events(app_name, limit=10)
                if not events:
                    print("    no events recorded")
                else:
                    for event in events:
                        timestamp = event.created_at.strftime("%Y-%m-%d %H:%M:%S")
                        print(
                            f"    event {timestamp} rev={event.revision} "
                            f"{event.event_type}: {event.message}"
                        )
            if args.wide and runtime is not None:
                try:
                    infos = runtime.list_containers_info()  # type: ignore[attr-defined]
                except Exception:
                    infos = []
                if infos:
                    filtered = [
                        c for c in infos if (c.get("labels") or {}).get("ae.app") == app_name
                    ]
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

        if args.watch:
            import time

            start = time.time()
            while True:
                _print_status_block(status)
                ready = status.ready_replicas >= status.desired_replicas
                if ready:
                    return 0
                if args.timeout and (time.time() - start) > args.timeout:
                    return 1
                time.sleep(args.watch)
                status = store.get_status(app_name)
                if status is None:
                    print(f"No status recorded for {_display_app_name(app_name)}")
                    return 1
        else:
            _print_status_block(status)
        return 0
    statuses = store.list_status()
    if ns_filter:
        statuses = [s for s in statuses if split_app_key(s.app_name)[0] == ns_filter]
    if not statuses:
        print("No workloads recorded.")
        return 0
    if args.json:
        import json

        def as_dict(s: AppStatus) -> dict:
            ns, name = split_app_key(s.app_name)
            d = {
                "app_name": s.app_name,
                "name": name,
                "namespace": ns,
                "deployment": name,
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
    app = _resolve_app_name(args.name, getattr(args, "namespace", None)) or args.name
    revs = store.list_revisions(app, limit=1)
    if not revs:
        print(f"no revisions recorded for {_display_app_name(app)}")
        return 1
    man = store.get_revision_manifest(app, revs[0].revision)
    rollout = dict(getattr(man.spec, "rollout", {}) or {})
    rollout["pause"] = True if args.rollout_cmd == "pause" else False
    new_spec = man.spec.model_copy(update={"rollout": rollout})
    updated = man.model_copy(update={"spec": new_spec})
    try:
        existing = store.get_registered_entry(app)
        src = existing.source if existing else "cli"
        lbls = existing.labels if existing else getattr(updated.metadata, "labels", None)
        store.register_app(updated, source=src, labels=lbls)
    except Exception:
        pass
    report = reconciler.reconcile(updated)
    print(
        f"rollout {args.rollout_cmd} {_display_app_name(app)}: rev={report.revision} status={report.revision_status} ready={report.ready_replicas}/{new_spec.replicas}"
    )
    return 0


def handle_api(args: argparse.Namespace) -> int:
    if args.api_cmd == "tokens" and (
        getattr(args, "generate", False) or getattr(args, "rotate", False)
    ):
        import secrets
        from datetime import datetime, timedelta

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
                (datetime.now(UTC) + timedelta(hours=int(hours))).isoformat().replace("+00:00", "Z")
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
                "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                "admin": {"token": admin, "expires": admin_exp},
                "scaler": {"token": scaler, "expires": scaler_exp},
                "read": {"token": reader, "expires": read_exp},
            }
            Path(state_path).parent.mkdir(parents=True, exist_ok=True)
            Path(state_path).write_text(_json.dumps(payload, indent=2))
        return 0
    print("unsupported api command")
    return 2


def _read_env_file_var(path: Path, key: str) -> str | None:
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        if k.strip() != key:
            continue
        val = v.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in {'"', "'"}:
            val = val[1:-1]
        return val
    return None


def _write_export_lines(lines: list[str], dest: Path | None) -> None:
    payload = "\n".join(lines) + ("\n" if lines else "")
    if dest:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(payload)
    else:
        print(payload, end="")


def _read_proc_env(pid: int) -> dict[str, str]:
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes()
    except Exception:
        return {}
    out: dict[str, str] = {}
    for entry in raw.split(b"\x00"):
        if not entry:
            continue
        try:
            k, v = entry.split(b"=", 1)
        except ValueError:
            continue
        try:
            out[k.decode("utf-8")] = v.decode("utf-8")
        except Exception:
            continue
    return out


def _profile_env_from_state_db(state_db: str | None) -> Path | None:
    if not state_db:
        return None
    try:
        db_path = Path(state_db).expanduser()
        if not db_path.is_absolute():
            db_path = Path.cwd() / db_path
        profile_dir = db_path.parent
        if profile_dir.parent.name != "profiles":
            return None
        candidate = profile_dir / "apishim.env"
        return candidate if candidate.exists() else None
    except Exception:
        return None


def _profile_state_db_from_env(apishim_env: Path | None) -> Path | None:
    if not apishim_env:
        return None
    try:
        profile_dir = apishim_env.expanduser().parent
        if profile_dir.parent.name != "profiles":
            return None
        return profile_dir / "controller.db"
    except Exception:
        return None


def _profile_name_from_env(apishim_env: Path | None) -> str | None:
    if not apishim_env:
        return None
    try:
        profile_dir = apishim_env.expanduser().parent
        if profile_dir.parent.name != "profiles":
            return None
        return profile_dir.name
    except Exception:
        return None


def _profile_etcd_defaults(profile_name: str | None) -> dict[str, str]:
    if not profile_name:
        return {}
    if profile_name in {"dev-etcd", "k1s-core"}:
        return {
            "AE_STATE_BACKEND": "etcd",
            "AE_ETCD_ENDPOINTS": "http://127.0.0.1:2379",
            "AE_ETCD_PREFIX": f"k1s/profiles/{profile_name}",
        }
    return {}


def _running_in_container() -> bool:
    if Path("/.dockerenv").exists() or Path("/run/.containerenv").exists():
        return True
    try:
        text = Path("/proc/1/cgroup").read_text(encoding="utf-8")
    except Exception:
        return False
    return any(tok in text for tok in ("docker", "podman", "containerd", "kubepods"))


def _normalize_upstream_server_for_host(server: str, port_hint: str | None = None) -> str:
    if not server:
        return server
    if _running_in_container():
        return server
    try:
        if "://" not in server:
            host = server
            path = ""
            if "/" in server:
                host, rest = server.split("/", 1)
                path = "/" + rest
            host_part, sep, port = host.partition(":")
            if host_part != "apishim":
                return server
            final_port = port
            if not final_port and port_hint and port_hint.isdigit():
                final_port = port_hint
            if final_port:
                return f"https://127.0.0.1:{final_port}{path}"
            return f"https://127.0.0.1:8445{path}"

        parsed = urlparse(server)
        if parsed.hostname != "apishim":
            return server
        port = parsed.port
        if port is None and port_hint and port_hint.isdigit():
            port = int(port_hint)
        netloc = "127.0.0.1"
        if port:
            netloc = f"{netloc}:{port}"
        return urlunparse(
            (parsed.scheme, netloc, parsed.path, parsed.params, parsed.query, parsed.fragment)
        )
    except Exception:
        return server


def _latest_profile_apishim_env(root: Path) -> Path | None:
    try:
        envs = [p for p in root.glob("*/apishim.env") if p.is_file()]
    except Exception:
        return None
    if not envs:
        return None
    try:
        envs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    except Exception:
        return envs[0]
    return envs[0]


def _detect_apishim_env(
    explicit: Path | None, controller_env: Path, proc_env: dict[str, str] | None = None
) -> Path:
    if explicit:
        return explicit
    env_override = os.getenv("APISHIM_ENV_FILE")
    if env_override:
        return Path(env_override)

    state_db = os.getenv("AE_STATE_DB")
    candidate = _profile_env_from_state_db(state_db)
    if candidate:
        return candidate

    controller_state_db = _read_env_file_var(controller_env, "AE_STATE_DB")
    candidate = _profile_env_from_state_db(controller_state_db)
    if candidate:
        return candidate

    if proc_env:
        candidate = _profile_env_from_state_db(proc_env.get("AE_APISHIM_DB"))
        if candidate:
            return candidate

    root = Path("state/profiles")
    latest = _latest_profile_apishim_env(root)
    if latest:
        return latest
    return Path("state/profiles/labs/apishim.env")


def handle_auth(
    args: argparse.Namespace, global_args: argparse.Namespace | None = None
) -> int:
    if args.auth_cmd == "local":
        controller_env = Path(
            args.controller_env
            if args.controller_env
            else os.getenv("CONTROLLER_ENV_FILE", "state/env.sh")
        )
        dev_env = Path(args.dev_env if args.dev_env else os.getenv("DEV_ENV_FILE", "state/dev.env"))
        apishim_pid = Path(
            args.apishim_pid
            if args.apishim_pid
            else os.getenv("APISHIM_PID_FILE", "state/apishim.pid")
        )

        def pick(*vals: str | None) -> str:
            for val in vals:
                if val:
                    return val
            return ""

        proc_env: dict[str, str] = {}
        if apishim_pid.exists():
            try:
                pid_val = int(apishim_pid.read_text().strip() or "0")
            except Exception:
                pid_val = 0
            if pid_val > 0:
                proc_env = _read_proc_env(pid_val)

        apishim_env = _detect_apishim_env(
            args.apishim_env if args.apishim_env else None, controller_env, proc_env
        )
        profile_state_db_path = _profile_state_db_from_env(apishim_env)
        profile_state_db = None
        if profile_state_db_path:
            try:
                if profile_state_db_path.exists():
                    profile_state_db = str(profile_state_db_path)
            except Exception:
                profile_state_db = None
        profile_name = _profile_name_from_env(apishim_env)
        profile_etcd = _profile_etcd_defaults(profile_name)

        apishim_token = pick(
            proc_env.get("AE_APISHIM_TOKEN"),
            _read_env_file_var(apishim_env, "AE_APISHIM_TOKEN"),
            os.getenv("AE_APISHIM_TOKEN"),
        )
        apishim_read = pick(
            proc_env.get("AE_APISHIM_READ_TOKEN"),
            _read_env_file_var(apishim_env, "AE_APISHIM_READ_TOKEN"),
            os.getenv("AE_APISHIM_READ_TOKEN"),
        )
        apishim_exec = pick(
            proc_env.get("AE_APISHIM_EXEC_TOKEN"),
            _read_env_file_var(apishim_env, "AE_APISHIM_EXEC_TOKEN"),
            os.getenv("AE_APISHIM_EXEC_TOKEN"),
        )
        apishim_pf = pick(
            proc_env.get("AE_APISHIM_PORTFORWARD_TOKEN"),
            _read_env_file_var(apishim_env, "AE_APISHIM_PORTFORWARD_TOKEN"),
            os.getenv("AE_APISHIM_PORTFORWARD_TOKEN"),
        )
        apishim_secret = pick(
            proc_env.get("AE_APISHIM_SESSION_SECRET"),
            _read_env_file_var(apishim_env, "AE_APISHIM_SESSION_SECRET"),
            os.getenv("AE_APISHIM_SESSION_SECRET"),
        )
        admin_token = pick(
            proc_env.get("AE_API_ADMIN_TOKEN"),
            _read_env_file_var(apishim_env, "AE_API_ADMIN_TOKEN"),
            _read_env_file_var(controller_env, "AE_API_ADMIN_TOKEN"),
            os.getenv("AE_API_ADMIN_TOKEN"),
        )
        labs_token = pick(
            proc_env.get("AE_LABS_TOKEN"),
            _read_env_file_var(apishim_env, "AE_LABS_TOKEN"),
            _read_env_file_var(controller_env, "AE_LABS_TOKEN"),
            os.getenv("AE_LABS_TOKEN"),
        )
        scaler_token = pick(
            _read_env_file_var(controller_env, "AE_API_SCALER_TOKEN"),
            os.getenv("AE_API_SCALER_TOKEN"),
        )
        read_token = pick(
            _read_env_file_var(controller_env, "AE_API_READ_TOKEN"),
            os.getenv("AE_API_READ_TOKEN"),
        )
        state_db = pick(
            profile_state_db,
            _read_env_file_var(controller_env, "AE_STATE_DB"),
            os.getenv("AE_STATE_DB"),
        )
        state_backend = pick(
            profile_etcd.get("AE_STATE_BACKEND"),
            _read_env_file_var(controller_env, "AE_STATE_BACKEND"),
            os.getenv("AE_STATE_BACKEND"),
        )
        etcd_endpoints = pick(
            profile_etcd.get("AE_ETCD_ENDPOINTS"),
            _read_env_file_var(controller_env, "AE_ETCD_ENDPOINTS"),
            os.getenv("AE_ETCD_ENDPOINTS"),
        )
        etcd_prefix = pick(
            profile_etcd.get("AE_ETCD_PREFIX"),
            _read_env_file_var(controller_env, "AE_ETCD_PREFIX"),
            os.getenv("AE_ETCD_PREFIX"),
        )

        server = args.server or os.getenv("AE_APISHIM_SERVER") or proc_env.get("AE_APISHIM_SERVER")
        if not server:
            server = _read_env_file_var(controller_env, "AE_APISHIM_SERVER")
        server_from_upstream = False
        port_hint = None
        if not server:
            upstream = pick(
                os.getenv("APISHIM_UPSTREAM"),
                _read_env_file_var(dev_env, "APISHIM_UPSTREAM"),
            )
            port_hint = pick(
                os.getenv("APISHIM_PORT"),
                _read_env_file_var(dev_env, "APISHIM_PORT"),
            )
            if upstream:
                server_from_upstream = True
                server = upstream if "://" in upstream else f"https://{upstream}"
            elif port_hint:
                server = f"https://127.0.0.1:{port_hint}"
            else:
                server = "https://127.0.0.1:8445"
        if server and server_from_upstream:
            server = _normalize_upstream_server_for_host(server, port_hint)

        lines: list[str] = []
        if apishim_token:
            lines.append(f"export AE_APISHIM_TOKEN={apishim_token}")
        if apishim_read:
            lines.append(f"export AE_APISHIM_READ_TOKEN={apishim_read}")
        if apishim_exec:
            lines.append(f"export AE_APISHIM_EXEC_TOKEN={apishim_exec}")
        if apishim_pf:
            lines.append(f"export AE_APISHIM_PORTFORWARD_TOKEN={apishim_pf}")
        if apishim_secret:
            lines.append(f"export AE_APISHIM_SESSION_SECRET={apishim_secret}")
        if admin_token:
            lines.append(f"export AE_API_ADMIN_TOKEN={admin_token}")
        if labs_token:
            lines.append(f"export AE_LABS_TOKEN={labs_token}")
        if scaler_token:
            lines.append(f"export AE_API_SCALER_TOKEN={scaler_token}")
        if read_token:
            lines.append(f"export AE_API_READ_TOKEN={read_token}")
        if server:
            lines.append(f"export AE_APISHIM_SERVER={server}")
        if state_backend:
            lines.append(f"export AE_STATE_BACKEND={state_backend}")
        if etcd_endpoints:
            lines.append(f"export AE_ETCD_ENDPOINTS={etcd_endpoints}")
        if etcd_prefix:
            lines.append(f"export AE_ETCD_PREFIX={etcd_prefix}")
        if state_db:
            lines.append(f"export AE_STATE_DB={state_db}")
        _write_export_lines(lines, getattr(args, "output", None))
        return 0

    if args.auth_cmd == "remote":
        import secrets

        apishim_token = secrets.token_urlsafe(32)
        apishim_read = secrets.token_urlsafe(32)
        apishim_secret = secrets.token_urlsafe(32)
        admin_token = secrets.token_hex(16)
        scaler_token = secrets.token_hex(16)
        read_token = secrets.token_hex(16)
        api_server = os.getenv("AE_API_SERVER") or (
            str(getattr(global_args, "server", ""))
            if global_args and getattr(global_args, "server", None)
            else None
        )

        lines = [
            f"export AE_APISHIM_TOKEN={apishim_token}",
            f"export AE_APISHIM_READ_TOKEN={apishim_read}",
            f"export AE_APISHIM_SESSION_SECRET={apishim_secret}",
            f"export AE_API_ADMIN_TOKEN={admin_token}",
            f"export AE_API_SCALER_TOKEN={scaler_token}",
            f"export AE_API_READ_TOKEN={read_token}",
        ]
        if api_server:
            lines.append(f"export AE_API_SERVER={api_server}")
        if not getattr(args, "no_mutations", False):
            lines.append("export AE_API_MUTATIONS=1")
        _write_export_lines(lines, getattr(args, "output", None))
        return 0

    print("unsupported auth command")
    return 2


def handle_tls(args: argparse.Namespace) -> int:
    # tls sync: copy optional input and resolve to PEM
    if args.tls_cmd == "sync":
        import os

        from ae.ingress.tls_sync import TlsSecretResolver

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
        import os

        from ae.ingress.tls_sync import TlsSecretResolver

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
    # tls kubesecret: emit kubernetes.io/tls Secret YAML from resolved PEMs
    if args.tls_cmd == "kubesecret":
        import base64 as _b64
        import os

        import yaml as _yaml

        from ae.ingress.tls_sync import TlsSecretResolver

        root = Path(args.root) if args.root else Path(os.getenv("AE_TLS_DIR", "state/tls"))
        resolver = TlsSecretResolver(root)
        resolved = resolver.resolve(str(args.name))
        if not resolved:
            print(
                f"not found: {args.name} under {root}. Use 'ae tls sync' or place <name>.crt/.key first."
            )
            return 2
        crt, key = resolved
        try:
            crt_b64 = _b64.b64encode(Path(crt).read_bytes()).decode("ascii")
            key_b64 = _b64.b64encode(Path(key).read_bytes()).decode("ascii")
        except Exception as exc:  # noqa: BLE001
            print(f"failed to read TLS PEMs: {exc}")
            return 1
        doc = {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": str(args.name), "namespace": str(args.namespace)},
            "type": "kubernetes.io/tls",
            "data": {"tls.crt": crt_b64, "tls.key": key_b64},
        }
        out = _yaml.safe_dump(doc, sort_keys=False)
        dest = getattr(args, "output", None)
        if dest:
            Path(dest).parent.mkdir(parents=True, exist_ok=True)
            Path(dest).write_text(out, encoding="utf-8")
        else:
            print(out, end="")
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
    app_name = _resolve_app_name(args.name, getattr(args, "namespace", None)) or args.name
    status = store.get_status(app_name)
    if status is None:
        print(f"No status recorded for {_display_app_name(app_name)}")
        return 1
    pods = store.list_pods(app_name)
    if not pods:
        print(f"No pods available for {_display_app_name(app_name)}")
        return 1
    # optional revision filter
    if args.revision is not None:
        rev_tag = f"-rev{args.revision}-"
        pods = [r for r in pods if rev_tag in r.pod_name]
        if not pods:
            print(f"No pods for {_display_app_name(app_name)} at revision {args.revision}")
            return 1

    # select by container flag
    target = None
    if args.container is not None:
        sel = str(args.container)
        if sel.isdigit():
            # match by replica index suffix
            suffix = f"-{sel}"
            for r in pods:
                if r.pod_name.endswith(suffix):
                    target = r
                    break
        else:
            # exact match or contains
            for r in pods:
                if r.pod_name == sel or sel in r.pod_name:
                    target = r
                    break
        if target is None:
            print(f"No matching pod for --container={sel}")
            return 1
    else:
        # prefer a ready pod, otherwise first
        target = next((r for r in pods if r.ready), pods[0])

    since_seconds = _parse_since_secs(args.since) if args.since else None
    if since_seconds is None and args.since_time:
        since_seconds = _parse_rfc3339_to_epoch(args.since_time)

    for line in runtime.read_logs(
        target.pod_name,
        follow=args.follow,
        tail=args.tail,
        since=since_seconds,
    ):
        print(line)
    return 0


def _resolve_exec_target(
    store: SQLiteStateStore, app_name: str, container_sel: str | None
) -> tuple[str | None, str | None]:
    status = store.get_status(app_name)
    pods = store.list_pods(app_name) if status is not None else []
    pod_name: str | None = None
    container_name = container_sel
    if pods:
        if container_sel:
            sel = str(container_sel)
            if sel.isdigit():
                suffix = f"-{sel}"
                for r in pods:
                    if r.pod_name.endswith(suffix):
                        pod_name = r.pod_name
                        container_name = None
                        break
            if pod_name is None:
                for r in pods:
                    if r.pod_name == sel or sel in r.pod_name:
                        pod_name = r.pod_name
                        container_name = None
                        break
        if pod_name is None:
            target = next((r for r in pods if r.ready), pods[0])
            pod_name = target.pod_name
    return pod_name, container_name


def _exec_over_spdy(
    base: str,
    *,
    namespace: str,
    pod_name: str,
    command: list[str],
    container: str | None,
    stdin: bool,
    stdout: bool,
    stderr: bool,
    tty: bool,
    token: str | None,
    timeout: int | None,
) -> int:
    import base64
    import json
    import os
    import shutil
    import signal
    import socket
    import ssl
    import sys
    import threading
    import urllib.parse
    import zlib
    from contextlib import contextmanager

    if "://" not in base:
        base = "http://" + base
    parsed = urllib.parse.urlparse(base)
    scheme = parsed.scheme or "http"
    host = parsed.hostname or ""
    port = parsed.port or (443 if scheme == "https" else 80)
    if not host:
        raise RuntimeError(f"invalid apishim base: {base}")
    base_path = parsed.path.rstrip("/")
    path = f"{base_path}/api/v1/namespaces/{namespace}/pods/{pod_name}/exec"
    params: dict[str, list[str]] = {
        "command": [str(c) for c in command],
        "stdin": ["1" if stdin else "0"],
        "stdout": ["1" if stdout else "0"],
        "stderr": ["1" if stderr else "0"],
        "tty": ["1" if tty else "0"],
    }
    if container:
        params["container"] = [container]
    query = urllib.parse.urlencode(params, doseq=True)
    full_path = path + ("?" + query if query else "")

    sock = socket.create_connection((host, port), timeout=timeout or 10)
    if scheme == "https":
        ctx = ssl.create_default_context()
        if os.getenv("AE_APISHIM_INSECURE") == "1":
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        sock = ctx.wrap_socket(sock, server_hostname=host)

    req_lines = [
        f"POST {full_path} HTTP/1.1",
        f"Host: {host}:{port}",
        "Connection: Upgrade",
        "Upgrade: SPDY/3.1",
        "X-Stream-Protocol-Version: v5.channel.k8s.io, v4.channel.k8s.io, v3.channel.k8s.io, v2.channel.k8s.io, channel.k8s.io",
        "Content-Length: 0",
    ]
    if token:
        req_lines.append(f"Authorization: Bearer {token}")
    req_lines.append("\r\n")
    sock.sendall(("\r\n".join(req_lines)).encode("utf-8"))

    def _recv_until(marker: bytes, limit: int = 65536) -> bytes:
        buf = b""
        while marker not in buf and len(buf) < limit:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
        return buf

    header = _recv_until(b"\r\n\r\n")
    if b"\r\n" not in header:
        raise RuntimeError("spdy: invalid response from server")
    status_line, rest = header.split(b"\r\n", 1)
    try:
        status_code = int(status_line.split()[1])
    except Exception:
        status_code = 0
    if status_code != 101:
        try:
            body = rest.split(b"\r\n\r\n", 1)[1]
            msg = body.decode("utf-8", "ignore").strip()
        except Exception:
            msg = ""
        raise RuntimeError(f"spdy upgrade failed: {status_code} {msg}".strip())

    SPDY_DICT = base64.b64decode(
        "AAAAB29wdGlvbnMAAAAEaGVhZAAAAARwb3N0AAAAA3B1dAAAAAZkZWxldGUAAAAFdHJhY2UAAAAGYWNjZXB0AAAADmFjY2VwdC1jaGFyc2V0AAAAD2FjY2VwdC1lbmNvZGluZwAAAA9hY2NlcHQtbGFuZ3VhZ2UAAAANYWNjZXB0LXJhbmdlcwAAAANhZ2UAAAAFYWxsb3cAAAANYXV0aG9yaXphdGlvbgAAAA1jYWNoZS1jb250cm9sAAAACmNvbm5lY3Rpb24AAAAMY29udGVudC1iYXNlAAAAEGNvbnRlbnQtZW5jb2RpbmcAAAAQY29udGVudC1sYW5ndWFnZQAAAA5jb250ZW50LWxlbmd0aAAAABBjb250ZW50LWxvY2F0aW9uAAAAC2NvbnRlbnQtbWQ1AAAADWNvbnRlbnQtcmFuZ2UAAAAMY29udGVudC10eXBlAAAABGRhdGUAAAAEZXRhZwAAAAZleHBlY3QAAAAHZXhwaXJlcwAAAARmcm9tAAAABGhvc3QAAAAIaWYtbWF0Y2gAAAARaWYtbW9kaWZpZWQtc2luY2UAAAANaWYtbm9uZS1tYXRjaAAAAAhpZi1yYW5nZQAAABNpZi11bm1vZGlmaWVkLXNpbmNlAAAADWxhc3QtbW9kaWZpZWQAAAAIbG9jYXRpb24AAAAMbWF4LWZvcndhcmRzAAAABnByYWdtYQAAABJwcm94eS1hdXRoZW50aWNhdGUAAAATcHJveHktYXV0aG9yaXphdGlvbgAAAAVyYW5nZQAAAAdyZWZlcmVyAAAAC3JldHJ5LWFmdGVyAAAABnNlcnZlcgAAAAJ0ZQAAAAd0cmFpbGVyAAAAEXRyYW5zZmVyLWVuY29kaW5nAAAAB3VwZ3JhZGUAAAAKdXNlci1hZ2VudAAAAAR2YXJ5AAAAA3ZpYQAAAAd3YXJuaW5nAAAAEHd3dy1hdXRoZW50aWNhdGUAAAAGbWV0aG9kAAAAA2dldAAAAAZzdGF0dXMAAAAGMjAwIE9LAAAAB3ZlcnNpb24AAAAISFRUUC8xLjEAAAADdXJsAAAABnB1YmxpYwAAAApzZXQtY29va2llAAAACmtlZXAtYWxpdmUAAAAGb3JpZ2luMTAwMTAxMjAxMjAyMjA1MjA2MzAwMzAyMzAzMzA0MzA1MzA2MzA3NDAyNDA1NDA2NDA3NDA4NDA5NDEwNDExNDEyNDEzNDE0NDE1NDE2NDE3NTAyNTA0NTA1MjAzIE5vbi1BdXRob3JpdGF0aXZlIEluZm9ybWF0aW9uMjA0IE5vIENvbnRlbnQzMDEgTW92ZWQgUGVybWFuZW50bHk0MDAgQmFkIFJlcXVlc3Q0MDEgVW5hdXRob3JpemVkNDAzIEZvcmJpZGRlbjQwNCBOb3QgRm91bmQ1MDAgSW50ZXJuYWwgU2VydmVyIEVycm9yNTAxIE5vdCBJbXBsZW1lbnRlZDUwMyBTZXJ2aWNlIFVuYXZhaWxhYmxlSmFuIEZlYiBNYXIgQXByIE1heSBKdW4gSnVsIEF1ZyBTZXB0IE9jdCBOb3YgRGVjIDAwOjAwOjAwIE1vbiwgVHVlLCBXZWQsIFRodSwgRnJpLCBTYXQsIFN1biwgR01UY2h1bmtlZCx0ZXh0L2h0bWwsaW1hZ2UvcG5nLGltYWdlL2pwZyxpbWFnZS9naWYsYXBwbGljYXRpb24veG1sLGFwcGxpY2F0aW9uL3hodG1sK3htbCx0ZXh0L3BsYWluLHRleHQvamF2YXNjcmlwdCxwdWJsaWNwcml2YXRlbWF4LWFnZT1nemlwLGRlZmxhdGUsc2RjaGNoYXJzZXQ9dXRmLThjaGFyc2V0PWlzby04ODU5LTEsdXRmLSwqLGVucT0wLg=="
    )
    cctx = zlib.compressobj(wbits=15, zdict=SPDY_DICT)

    send_lock = threading.Lock()

    def _send_bytes(data: bytes) -> None:
        with send_lock:
            sock.sendall(data)

    def _encode_headers(headers: dict[str, str]) -> bytes:
        buf = bytearray()
        buf += len(headers).to_bytes(4, "big")
        for name, value in headers.items():
            n = name.encode("utf-8")
            v = value.encode("utf-8")
            buf += len(n).to_bytes(4, "big")
            buf += n
            buf += len(v).to_bytes(4, "big")
            buf += v
        return cctx.compress(bytes(buf)) + cctx.flush(zlib.Z_SYNC_FLUSH)

    def _send_ctrl(frame_type: int, flags: int, payload: bytes) -> None:
        header = bytearray()
        header += b"\x80\x03"
        header += frame_type.to_bytes(2, "big")
        header.append(flags & 0xFF)
        header += len(payload).to_bytes(3, "big")
        _send_bytes(bytes(header) + payload)

    def _send_syn_stream(stream_id: int, headers: dict[str, str]) -> None:
        hdrs = _encode_headers(headers)
        payload = (
            (stream_id & 0x7FFFFFFF).to_bytes(4, "big") + b"\x00\x00\x00\x00" + b"\x00\x00" + hdrs
        )
        _send_ctrl(1, 0, payload)

    def _send_data(stream_id: int, data: bytes, fin: bool = False) -> None:
        header = bytearray()
        header += (stream_id & 0x7FFFFFFF).to_bytes(4, "big")
        header.append(0x02 if fin else 0x00)
        header += len(data).to_bytes(3, "big")
        _send_bytes(bytes(header) + data)

    stream_ids: dict[str, int] = {}
    next_sid = 1

    def _alloc(name: str) -> int:
        nonlocal next_sid
        sid = next_sid
        next_sid += 2
        stream_ids[name] = sid
        return sid

    _alloc("error")
    if stdin:
        _alloc("stdin")
    if stdout:
        _alloc("stdout")
    if stderr and not tty:
        _alloc("stderr")
    if tty:
        _alloc("resize")

    for stype, sid in stream_ids.items():
        _send_syn_stream(
            sid,
            {
                ":method": "POST",
                ":path": full_path,
                ":version": "HTTP/1.1",
                ":host": host,
                "streamtype": stype,
            },
        )

    stop_event = threading.Event()
    exit_code = 0
    err_buf = b""

    @contextmanager
    def _maybe_raw() -> None:
        if not tty or not sys.stdin.isatty():
            yield
            return
        import termios
        import tty as _tty

        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            _tty.setraw(fd)
            yield
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)

    def _pump_stdin() -> None:
        if "stdin" not in stream_ids:
            return
        fd = sys.stdin.fileno()
        while not stop_event.is_set():
            try:
                data = os.read(fd, 1024)
            except Exception:
                break
            if not data:
                try:
                    _send_data(stream_ids["stdin"], b"", fin=True)
                except Exception:
                    pass
                break
            try:
                _send_data(stream_ids["stdin"], data, fin=False)
            except Exception:
                break

    def _send_resize() -> None:
        if "resize" not in stream_ids:
            return
        try:
            cols, rows = shutil.get_terminal_size(fallback=(80, 24))
            payload = json.dumps({"Width": cols, "Height": rows}).encode("utf-8")
            _send_data(stream_ids["resize"], payload, fin=False)
        except Exception:
            pass

    stdin_thread = None
    old_handler = None
    if tty and "resize" in stream_ids and sys.stdin.isatty():
        old_handler = signal.getsignal(signal.SIGWINCH)
        signal.signal(signal.SIGWINCH, lambda *_args: _send_resize())
        _send_resize()

    with _maybe_raw():
        if stdin and "stdin" in stream_ids:
            stdin_thread = threading.Thread(target=_pump_stdin, daemon=True)
            stdin_thread.start()

        buf = b""
        stream_by_id = {sid: name for name, sid in stream_ids.items()}
        while not stop_event.is_set():
            try:
                chunk = sock.recv(4096)
            except Exception:
                break
            if not chunk:
                break
            buf += chunk
            while True:
                if len(buf) < 8:
                    break
                hdr = buf[:8]
                is_control = (hdr[0] & 0x80) != 0
                length = int.from_bytes(hdr[5:8], "big")
                frame_len = 8 + length
                if len(buf) < frame_len:
                    break
                payload = buf[8:frame_len]
                buf = buf[frame_len:]
                if is_control:
                    frame_type = int.from_bytes(hdr[2:4], "big")
                    if frame_type == 6:  # PING
                        try:
                            _send_bytes(hdr + payload)
                        except Exception:
                            pass
                    if frame_type == 7:  # GOAWAY
                        stop_event.set()
                        break
                else:
                    sid = int.from_bytes(hdr[0:4], "big") & 0x7FFFFFFF
                    flags = hdr[4]
                    stype = stream_by_id.get(sid)
                    if stype == "stdout" and stdout:
                        sys.stdout.buffer.write(payload)
                        sys.stdout.buffer.flush()
                    elif stype == "stderr" and stderr and not tty:
                        sys.stderr.buffer.write(payload)
                        sys.stderr.buffer.flush()
                    elif stype == "error":
                        err_buf += payload
                        if flags & 0x02:
                            try:
                                status = json.loads(err_buf.decode("utf-8", "ignore") or "{}")
                                exit_code = int(
                                    status.get("details", {}).get("exitCode")
                                    or status.get("code")
                                    or 0
                                )
                            except Exception:
                                exit_code = 0
                            stop_event.set()
                            break
            if stop_event.is_set():
                break

    stop_event.set()
    if stdin_thread:
        stdin_thread.join(timeout=1)
    if old_handler is not None:
        try:
            signal.signal(signal.SIGWINCH, old_handler)
        except Exception:
            pass
    try:
        sock.close()
    except Exception:
        pass
    return exit_code


def _exec_over_ws(
    base: str,
    *,
    namespace: str,
    pod_name: str,
    command: list[str],
    container: str | None,
    stdin: bool,
    stdout: bool,
    stderr: bool,
    tty: bool,
    token: str | None,
    timeout: int | None,
) -> int:
    import base64
    import json
    import os
    import shutil
    import signal
    import socket
    import ssl
    import sys
    import threading
    import urllib.parse
    from contextlib import contextmanager

    if "://" not in base:
        base = "http://" + base
    parsed = urllib.parse.urlparse(base)
    scheme = parsed.scheme or "http"
    host = parsed.hostname or ""
    port = parsed.port or (443 if scheme == "https" else 80)
    if not host:
        raise RuntimeError(f"invalid apishim base: {base}")
    base_path = parsed.path.rstrip("/")
    path = f"{base_path}/api/v1/namespaces/{namespace}/pods/{pod_name}/exec"
    params: dict[str, list[str]] = {
        "command": [str(c) for c in command],
        "stdin": ["1" if stdin else "0"],
        "stdout": ["1" if stdout else "0"],
        "stderr": ["1" if stderr else "0"],
        "tty": ["1" if tty else "0"],
    }
    if container:
        params["container"] = [container]
    query = urllib.parse.urlencode(params, doseq=True)
    full_path = path + ("?" + query if query else "")

    sock = socket.create_connection((host, port), timeout=timeout or 10)
    if scheme == "https":
        ctx = ssl.create_default_context()
        if os.getenv("AE_APISHIM_INSECURE") == "1":
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        sock = ctx.wrap_socket(sock, server_hostname=host)

    ws_key = base64.b64encode(os.urandom(16)).decode("ascii")
    req_lines = [
        f"GET {full_path} HTTP/1.1",
        f"Host: {host}:{port}",
        "Upgrade: websocket",
        "Connection: Upgrade",
        "Sec-WebSocket-Version: 13",
        f"Sec-WebSocket-Key: {ws_key}",
        "Sec-WebSocket-Protocol: v5.channel.k8s.io",
    ]
    if token:
        req_lines.append(f"Authorization: Bearer {token}")
    req_lines.append("\r\n")
    sock.sendall(("\r\n".join(req_lines)).encode("utf-8"))

    def _recv_until(marker: bytes, limit: int = 65536) -> bytes:
        buf = b""
        while marker not in buf and len(buf) < limit:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
        return buf

    header = _recv_until(b"\r\n\r\n")
    if b"\r\n" not in header:
        raise RuntimeError("websocket: invalid response from server")
    status_line = header.split(b"\r\n", 1)[0]
    try:
        status_code = int(status_line.split()[1])
    except Exception:
        status_code = 0
    if status_code != 101:
        raise RuntimeError(f"websocket upgrade failed: {status_code}")

    send_lock = threading.Lock()

    def _send_ws(payload: bytes, opcode: int = 0x2) -> None:
        mask_key = os.urandom(4)
        header = bytearray()
        header.append(0x80 | (opcode & 0x0F))
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < (1 << 16):
            header.append(0x80 | 126)
            header.extend(length.to_bytes(2, "big"))
        else:
            header.append(0x80 | 127)
            header.extend(length.to_bytes(8, "big"))
        masked = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
        with send_lock:
            sock.sendall(bytes(header) + mask_key + masked)

    def _recv_exact(n: int) -> bytes | None:
        buf = b""
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                return None
            buf += chunk
        return buf

    def _recv_ws() -> tuple[int, bytes] | None:
        hdr = _recv_exact(2)
        if not hdr:
            return None
        opcode = hdr[0] & 0x0F
        length = hdr[1] & 0x7F
        if length == 126:
            ext = _recv_exact(2)
            if ext is None:
                return None
            length = int.from_bytes(ext, "big")
        elif length == 127:
            ext = _recv_exact(8)
            if ext is None:
                return None
            length = int.from_bytes(ext, "big")
        payload = _recv_exact(length) if length else b""
        if payload is None:
            return None
        return opcode, payload

    stop_event = threading.Event()
    exit_code = 0
    err_buf = b""

    @contextmanager
    def _maybe_raw() -> None:
        if not tty or not sys.stdin.isatty():
            yield
            return
        import termios
        import tty as _tty

        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            _tty.setraw(fd)
            yield
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)

    def _send_channel(ch: int, data: bytes) -> None:
        _send_ws(bytes([ch]) + data, opcode=0x2)

    def _pump_stdin() -> None:
        if not stdin:
            return
        fd = sys.stdin.fileno()
        while not stop_event.is_set():
            try:
                data = os.read(fd, 1024)
            except Exception:
                break
            if not data:
                break
            _send_channel(0, data)

    def _send_resize() -> None:
        if not tty:
            return
        try:
            cols, rows = shutil.get_terminal_size(fallback=(80, 24))
            payload = json.dumps({"Width": cols, "Height": rows}).encode("utf-8")
            _send_channel(4, payload)
        except Exception:
            pass

    stdin_thread = None
    old_handler = None
    if tty and sys.stdin.isatty():
        old_handler = signal.getsignal(signal.SIGWINCH)
        signal.signal(signal.SIGWINCH, lambda *_args: _send_resize())
        _send_resize()

    with _maybe_raw():
        if stdin:
            stdin_thread = threading.Thread(target=_pump_stdin, daemon=True)
            stdin_thread.start()
        while not stop_event.is_set():
            msg = _recv_ws()
            if msg is None:
                break
            opcode, payload = msg
            if opcode == 0x8:
                break
            if not payload:
                continue
            ch = payload[0]
            data = payload[1:]
            if ch == 1 and stdout:
                sys.stdout.buffer.write(data)
                sys.stdout.buffer.flush()
            elif ch == 2 and stderr and not tty:
                sys.stderr.buffer.write(data)
                sys.stderr.buffer.flush()
            elif ch == 3:
                err_buf += data
                try:
                    status = json.loads(err_buf.decode("utf-8", "ignore") or "{}")
                    exit_code = int(
                        status.get("details", {}).get("exitCode") or status.get("code") or 0
                    )
                except Exception:
                    exit_code = 0
                stop_event.set()
                break

    stop_event.set()
    if stdin_thread:
        stdin_thread.join(timeout=1)
    if old_handler is not None:
        try:
            signal.signal(signal.SIGWINCH, old_handler)
        except Exception:
            pass
    try:
        sock.close()
    except Exception:
        pass
    return exit_code


def _parse_pf_mapping(mapping: str) -> tuple[int, int]:
    raw = str(mapping or "").strip()
    if not raw:
        raise ValueError("port mapping is required (local:remote)")
    if ":" in raw:
        left, right = raw.split(":", 1)
    else:
        left, right = raw, raw
    left = left.strip()
    right = right.strip()
    if not left or not right:
        raise ValueError("port mapping must be in local:remote form")
    return int(left), int(right)


def _portforward_over_ws(
    base: str,
    *,
    namespace: str,
    pod_name: str,
    local_host: str,
    local_port: int,
    remote_port: int,
    token: str | None,
    timeout: int | None,
) -> int:
    import base64
    import os
    import signal
    import socket
    import ssl
    import sys
    import threading
    import urllib.parse

    if "://" not in base:
        base = "http://" + base
    parsed = urllib.parse.urlparse(base)
    scheme = parsed.scheme or "http"
    host = parsed.hostname or ""
    port = parsed.port or (443 if scheme == "https" else 80)
    if not host:
        raise RuntimeError(f"invalid apishim base: {base}")
    base_path = parsed.path.rstrip("/")
    path = f"{base_path}/api/v1/namespaces/{namespace}/pods/{pod_name}/portforward"
    query = urllib.parse.urlencode({"ports": str(remote_port), "token": token or ""})
    full_path = path + ("?" + query if query else "")

    stop_event = threading.Event()

    def _open_ws() -> socket.socket:
        sock = socket.create_connection((host, port), timeout=timeout or 10)
        if scheme == "https":
            ctx = ssl.create_default_context()
            if os.getenv("AE_APISHIM_INSECURE") == "1":
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            sock = ctx.wrap_socket(sock, server_hostname=host)
        ws_key = base64.b64encode(os.urandom(16)).decode("ascii")
        req_lines = [
            f"GET {full_path} HTTP/1.1",
            f"Host: {host}:{port}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            "Sec-WebSocket-Version: 13",
            f"Sec-WebSocket-Key: {ws_key}",
            "Sec-WebSocket-Protocol: portforward.k8s.io",
        ]
        if token:
            req_lines.append(f"Authorization: Bearer {token}")
        req_lines.append("\r\n")
        sock.sendall(("\r\n".join(req_lines)).encode("utf-8"))

        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
        if b"\r\n" not in buf:
            raise RuntimeError("websocket: invalid response from server")
        status_line = buf.split(b"\r\n", 1)[0]
        try:
            status_code = int(status_line.split()[1])
        except Exception:
            status_code = 0
        if status_code != 101:
            raise RuntimeError(f"websocket upgrade failed: {status_code}")
        try:
            sock.settimeout(0.2)
        except Exception:
            pass
        return sock

    def _send_ws(sock: socket.socket, payload: bytes) -> None:
        mask_key = os.urandom(4)
        header = bytearray()
        header.append(0x82)
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < (1 << 16):
            header.append(0x80 | 126)
            header.extend(length.to_bytes(2, "big"))
        else:
            header.append(0x80 | 127)
            header.extend(length.to_bytes(8, "big"))
        masked = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
        sock.sendall(bytes(header) + mask_key + masked)

    def _recv_exact(sock: socket.socket, n: int) -> bytes | None:
        buf = b""
        while len(buf) < n:
            try:
                chunk = sock.recv(n - len(buf))
            except TimeoutError:
                return None
            if not chunk:
                return None
            buf += chunk
        return buf

    def _recv_ws(sock: socket.socket) -> tuple[int, bytes] | None:
        hdr = _recv_exact(sock, 2)
        if not hdr:
            return None
        opcode = hdr[0] & 0x0F
        length = hdr[1] & 0x7F
        if length == 126:
            ext = _recv_exact(sock, 2)
            if ext is None:
                return None
            length = int.from_bytes(ext, "big")
        elif length == 127:
            ext = _recv_exact(sock, 8)
            if ext is None:
                return None
            length = int.from_bytes(ext, "big")
        payload = _recv_exact(sock, length) if length else b""
        if payload is None:
            return None
        return opcode, payload

    def _handle_conn(conn: socket.socket) -> None:
        conn_stop = threading.Event()
        try:
            ws = _open_ws()
        except Exception as exc:
            try:
                conn.close()
            except Exception:
                pass
            print(f"port-forward connect failed: {exc}")
            return

        def _pump_local() -> None:
            while not stop_event.is_set() and not conn_stop.is_set():
                try:
                    data = conn.recv(4096)
                except Exception:
                    break
                if not data:
                    break
                try:
                    _send_ws(ws, bytes([0]) + data)
                except Exception:
                    break
            conn_stop.set()

        def _pump_ws() -> None:
            while not stop_event.is_set() and not conn_stop.is_set():
                msg = _recv_ws(ws)
                if msg is None:
                    continue
                opcode, payload = msg
                if opcode == 0x8:
                    break
                if not payload:
                    continue
                ch = payload[0]
                data = payload[1:]
                if ch == 0 and data:
                    try:
                        conn.sendall(data)
                    except Exception:
                        break
                elif ch == 1 and data:
                    try:
                        sys.stderr.buffer.write(data)
                        sys.stderr.buffer.flush()
                    except Exception:
                        pass
            conn_stop.set()

        t1 = threading.Thread(target=_pump_local, daemon=True)
        t2 = threading.Thread(target=_pump_ws, daemon=True)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        try:
            ws.close()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind((local_host, local_port))
    listener.listen(5)
    listener.settimeout(0.5)

    print(f"Forwarding from {local_host}:{local_port} -> {pod_name}:{remote_port}")
    print("Press Ctrl+C to stop.")

    def _stop(_sig=None, _frame=None) -> None:  # noqa: ANN001 - signal handler
        stop_event.set()
        try:
            listener.close()
        except Exception:
            pass

    old_int = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, _stop)

    try:
        while not stop_event.is_set():
            try:
                conn, _addr = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            t = threading.Thread(target=_handle_conn, args=(conn,), daemon=True)
            t.start()
    finally:
        try:
            listener.close()
        except Exception:
            pass
        try:
            signal.signal(signal.SIGINT, old_int)
        except Exception:
            pass
    return 0


def handle_exec(args: argparse.Namespace, store: SQLiteStateStore, runtime: RuntimeAdapter) -> int:
    # Remote mode
    import inspect as _inspect

    frame = _inspect.currentframe()
    if frame is not None:
        outer_locals = frame.f_back.f_locals if frame.f_back else {}
        gargs = outer_locals.get("global_args") or outer_locals.get("args")
        import os as _os

        apishim_base = getattr(args, "apishim", None) or _os.getenv("AE_APISHIM_SERVER")
        ws_fallback = bool(
            getattr(args, "ws_fallback", False) or _os.getenv("AE_EXEC_WS_FALLBACK") == "1"
        )
        token = None
        if gargs is not None:
            token = getattr(gargs, "token", None)
        if token is None:
            token = _os.getenv("AE_APISHIM_TOKEN")
        if token is None:
            token = _os.getenv("AE_APISHIM_EXEC_TOKEN")
        if apishim_base:
            app_name = _resolve_app_name(args.name, getattr(args, "namespace", None)) or args.name
            cmd = list(args.cmd or [])
            if cmd and cmd[0] == "--":
                cmd = cmd[1:]
            if not cmd:
                print("exec requires a command after -- (e.g., ae exec app -- sh -c 'echo hi')")
                return 2
            pod_name, container_name = _resolve_exec_target(
                store, app_name, getattr(args, "container", None)
            )
            if pod_name is None:
                pod_name = app_name
                print("warning: no local status; using app name as pod reference")
            ns, _name = split_app_key(app_name)
            want_stdin = bool(getattr(args, "stdin", False))
            want_tty = bool(getattr(args, "tty", False))
            want_stderr = not want_tty
            try:
                return _exec_over_spdy(
                    apishim_base,
                    namespace=ns,
                    pod_name=pod_name,
                    command=cmd,
                    container=container_name,
                    stdin=want_stdin,
                    stdout=True,
                    stderr=want_stderr,
                    tty=want_tty,
                    token=token,
                    timeout=getattr(args, "timeout", None),
                )
            except Exception as exc:
                if ws_fallback:
                    print(f"spdy exec failed ({exc}); trying websocket fallback...")
                    try:
                        return _exec_over_ws(
                            apishim_base,
                            namespace=ns,
                            pod_name=pod_name,
                            command=cmd,
                            container=container_name,
                            stdin=want_stdin,
                            stdout=True,
                            stderr=want_stderr,
                            tty=want_tty,
                            token=token,
                            timeout=getattr(args, "timeout", None),
                        )
                    except Exception as exc2:
                        print(f"websocket exec failed: {exc2}")
                        return 1
                print(f"spdy exec failed: {exc}")
                return 1
        if gargs is not None and getattr(gargs, "server", None):
            return handle_exec_remote(args, gargs)

    # Local-only path
    if getattr(args, "stdin", False) or getattr(args, "tty", False):
        print("warning: --stdin/--tty are only supported against the API shim (SPDY/WebSocket)")
    app_name = _resolve_app_name(args.name, getattr(args, "namespace", None)) or args.name
    status = store.get_status(app_name)
    if status is None:
        print(f"No status recorded for {_display_app_name(app_name)}")
        return 1
    pods = store.list_pods(app_name)
    if not pods:
        print(f"No pods available for {_display_app_name(app_name)}")
        return 1
    timeout = getattr(args, "timeout", None)
    cmd = list(args.cmd or [])
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        print(
            "exec requires a command after -- (e.g., ae exec app --container sidecar -- sh -c 'echo hi')"
        )
        return 2
    if getattr(args, "container", None):
        cname = str(args.container)
        # If runtime supports container-scoped exec, use it
        if hasattr(runtime, "exec_for_container"):
            try:
                rc = int(runtime.exec_for_container(app_name, cname, cmd, timeout=timeout))
                return rc
            except Exception as exc:  # noqa: BLE001
                print(f"exec failed: {exc}")
                return 1
        # Fallback: select a pod by name substring
        target = next(
            (r for r in pods if (r.pod_name == cname or cname in r.pod_name)), None
        )
        if not target:
            print(f"No matching pod for --container={cname}")
            return 1
        return int(runtime.exec(target.pod_name, cmd, timeout=timeout))
    # Default: exec in a ready pod (main container context)
    target = next((r for r in pods if r.ready), pods[0])
    return int(runtime.exec(target.pod_name, cmd, timeout=timeout))


def handle_exec_remote(args: argparse.Namespace, global_args: argparse.Namespace) -> int:
    base = str(global_args.server)
    tok = getattr(global_args, "token", None)
    if getattr(args, "stdin", False) or getattr(args, "tty", False):
        print("warning: --stdin/--tty are not supported via controller exec; use --apishim")
    payload = {
        "container": getattr(args, "container", None),
        "cmd": [str(x) for x in (getattr(args, "cmd", []) or []) if x != "--"],
    }
    if getattr(args, "timeout", None) is not None:
        payload["timeoutSeconds"] = int(args.timeout)
    import requests

    app_name = _resolve_app_name(args.name, getattr(args, "namespace", None)) or args.name
    url = base.rstrip("/") + "/exec/" + app_name
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


def handle_shell(args: argparse.Namespace, store: SQLiteStateStore, runtime: RuntimeAdapter) -> int:
    import inspect as _inspect
    import os as _os
    import sys as _sys

    frame = _inspect.currentframe()
    gargs = None
    if frame is not None:
        outer_locals = frame.f_back.f_locals if frame.f_back else {}
        gargs = outer_locals.get("global_args") or outer_locals.get("args")

    apishim_base = getattr(args, "apishim", None) or _os.getenv("AE_APISHIM_SERVER")
    if not apishim_base and gargs and getattr(gargs, "server", None):
        apishim_base = gargs.server
    if not apishim_base:
        print("shell requires the API shim; set --apishim or AE_APISHIM_SERVER")
        return 2

    cmd = list(getattr(args, "cmd", []) or [])
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        cmd = ["sh"]

    exec_args = argparse.Namespace(**vars(args))
    exec_args.cmd = cmd
    exec_args.stdin = True
    if getattr(args, "no_tty", False):
        exec_args.tty = False
    elif getattr(args, "tty", False):
        exec_args.tty = True
    else:
        exec_args.tty = bool(_sys.stdin.isatty() and _sys.stdout.isatty())
    exec_args.apishim = apishim_base
    return handle_exec(exec_args, store, runtime)


def handle_port_forward(
    args: argparse.Namespace, store: SQLiteStateStore, runtime: RuntimeAdapter
) -> int:
    import inspect as _inspect
    import os as _os

    frame = _inspect.currentframe()
    gargs = None
    if frame is not None:
        outer_locals = frame.f_back.f_locals if frame.f_back else {}
        gargs = outer_locals.get("global_args") or outer_locals.get("args")

    apishim_base = getattr(args, "apishim", None) or _os.getenv("AE_APISHIM_SERVER")
    if not apishim_base and gargs and getattr(gargs, "server", None):
        apishim_base = gargs.server
    if not apishim_base:
        print("port-forward requires the API shim; set --apishim or AE_APISHIM_SERVER")
        return 2

    token = None
    if gargs is not None:
        token = getattr(gargs, "token", None)
    if token is None:
        token = _os.getenv("AE_APISHIM_PORTFORWARD_TOKEN")
    if token is None:
        token = _os.getenv("AE_APISHIM_TOKEN")

    try:
        local_port, remote_port = _parse_pf_mapping(getattr(args, "mapping", ""))
    except Exception as exc:  # noqa: BLE001
        print(f"invalid port mapping: {exc}")
        return 2

    app_name = _resolve_app_name(args.name, getattr(args, "namespace", None)) or args.name
    pod_name = getattr(args, "pod", None)
    if not pod_name:
        pod_name, _container = _resolve_exec_target(store, app_name, None)
    if not pod_name:
        pod_name = app_name
        print("warning: no local status; using app name as pod reference")

    ns, _name = split_app_key(app_name)
    bind_host = getattr(args, "bind", None) or "127.0.0.1"
    try:
        return _portforward_over_ws(
            apishim_base,
            namespace=ns,
            pod_name=pod_name,
            local_host=bind_host,
            local_port=local_port,
            remote_port=remote_port,
            token=token,
            timeout=None,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"port-forward failed: {exc}")
        return 1


def _status_to_json(status: AppStatus, store: SQLiteStateStore, *, include_details: bool) -> str:
    import json

    ns, name = split_app_key(status.app_name)
    data = {
        "app_name": status.app_name,
        "name": name,
        "namespace": ns,
        "deployment": name,
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

        s = value.strip()
        # Support trailing Z or offset like +00:00
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = _dt.datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_dt.UTC)
        return int(dt.timestamp())
    except Exception:
        return None


def handle_rollback(
    args: argparse.Namespace,
    store: SQLiteStateStore,
    reconciler: Reconciler,
) -> int:
    target_rev: int | None = args.to
    app_name = _resolve_app_name(args.name, getattr(args, "namespace", None)) or args.name
    if target_rev is None:
        revisions = store.list_revisions(app_name, limit=2)
        if len(revisions) < 2:
            print("No previous revision to roll back to.")
            return 1
        target_rev = revisions[1].revision

    try:
        manifest = store.get_revision_manifest(app_name, target_rev)
    except ValueError as exc:
        print(str(exc))
        return 1

    report = reconciler.reconcile(manifest)
    print(
        f"Rolled back {_display_app_name(app_name)} to revision {report.revision} ({report.revision_status})"
    )
    return 0


def handle_revisions(args: argparse.Namespace, store: SQLiteStateStore) -> int:
    app_name = _resolve_app_name(args.name, getattr(args, "namespace", None)) or args.name
    revisions = store.list_revisions(app_name, limit=args.limit)
    if not revisions:
        print(f"No revisions recorded for {_display_app_name(app_name)}.")
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
                    "total_pvs": snapshot.total_pvs,
                    "healthy_pvs": snapshot.healthy_pvs,
                    "unhealthy_pvs": snapshot.unhealthy_pvs,
                    "storage_used_bytes": snapshot.storage_used_bytes,
                    "storage_quota_bytes": snapshot.storage_quota_bytes,
                },
                indent=2,
            )
        )
        return 0

    print(
        f"apps total={snapshot.total_apps} ready={snapshot.ready_apps} progressing={snapshot.progressing_apps} degraded={snapshot.degraded_apps}"
    )
    print(
        f"replicas total={snapshot.total_replicas} ready={snapshot.ready_replicas} live={snapshot.live_replicas}"
    )
    if snapshot.total_pvs:
        print(
            f"volumes total={snapshot.total_pvs} healthy={snapshot.healthy_pvs} unhealthy={snapshot.unhealthy_pvs}"
        )
    if snapshot.storage_used_bytes or snapshot.storage_quota_bytes:
        for ns in sorted(set(snapshot.storage_used_bytes) | set(snapshot.storage_quota_bytes)):
            used = snapshot.storage_used_bytes.get(ns, 0)
            quota = snapshot.storage_quota_bytes.get(ns)
            if quota is not None:
                print(f"storage namespace={ns} used_bytes={used} quota_bytes={quota}")
            else:
                print(f"storage namespace={ns} used_bytes={used}")
    return 0


def handle_events(
    args: argparse.Namespace, store: SQLiteStateStore, global_args: argparse.Namespace
) -> int:
    app_name = _resolve_app_name(args.name, getattr(args, "namespace", None)) or args.name
    if getattr(global_args, "server", None):
        base = str(global_args.server)
        tok = getattr(global_args, "token", None)
        limit = getattr(args, "limit", 20)
        try:
            page = _http_get_json(base, f"/events/{app_name}?limit={int(limit)}", tok)
            items = page.get("items", []) if isinstance(page, dict) else page
            if not items:
                print(f"No events recorded for {_display_app_name(app_name)}.")
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
    events = store.list_events(app_name, limit=args.limit)
    if not events:
        print(f"No events recorded for {_display_app_name(app_name)}.")
        return 0
    for event in events:
        timestamp = event.created_at.strftime("%Y-%m-%d %H:%M:%S")
        print(f"{timestamp} rev={event.revision} {event.event_type}: {event.message}")
    return 0


def handle_services(args: argparse.Namespace, store: SQLiteStateStore) -> int:
    ns_filter = normalize_namespace(getattr(args, "namespace", None))
    rows = store.list_services()
    if ns_filter:
        rows = [r for r in rows if split_app_key(r.app_name)[0] == ns_filter]
    if args.json:
        import json as _json

        def _as_dict(s):
            ports = getattr(s, "ports", None)
            if ports is None:
                detail = store.get_service(s.app_name)
                ports = getattr(detail, "ports", {}) if detail else {}
            return {
                "app_name": s.app_name,
                "cluster_ip": s.cluster_ip,
                "ports": ports,
                "created_at": getattr(s, "created_at", None),
            }

        print(_json.dumps([_as_dict(s) for s in rows], indent=2))
        return 0
    if not rows:
        print("No services recorded.")
        return 0
    for svc in rows:
        ports = getattr(svc, "ports", None)
        if ports is None:
            detail = store.get_service(svc.app_name)
            ports = getattr(detail, "ports", {}) if detail else {}
        port_str = ", ".join(
            f"{p.get('name','')}:{p.get('port')}->{p.get('targetPort')}"
            for p in (ports or {}).get("ports", [])
        )
        print(f"{_display_app_name(svc.app_name)}: ip={svc.cluster_ip} ports={port_str}")
    return 0


def handle_history(
    args: argparse.Namespace, store: SQLiteStateStore, global_args: argparse.Namespace
) -> int:
    app_name = _resolve_app_name(args.name, getattr(args, "namespace", None)) or args.name
    # Server mode
    if getattr(global_args, "server", None):
        base = str(global_args.server)
        tok = getattr(global_args, "token", None)
        limit = int(getattr(args, "limit", 20))
        since_secs = (
            _parse_since_secs(getattr(args, "since", None))
            if getattr(args, "since", None)
            else None
        )
        since_ts = (
            _parse_rfc3339_to_epoch(getattr(args, "since_time", None))
            if getattr(args, "since_time", None)
            else None
        )
        query_limit = max(limit, 200) if (since_secs or since_ts) else limit
        try:
            items = _http_get_json(base, f"/history/{app_name}?limit={query_limit}", tok)
            if getattr(args, "json", False):
                import json as _json

                if since_secs or since_ts:
                    import time

                    cutoff = (time.time() - since_secs) if since_secs else float(since_ts)

                    def _keep(h):
                        try:
                            import datetime as _dt

                            t = _dt.datetime.fromisoformat(
                                h.get("check_time", "").replace("Z", "+00:00")
                            ).timestamp()
                            return t >= float(cutoff)
                        except Exception:
                            return True

                    items = [h for h in (items or []) if _keep(h)][:limit]
                print(_json.dumps(items, indent=2))
                return 0
            if not items:
                print(f"No probe history for {_display_app_name(app_name)}.")
                return 0
            rep = getattr(args, "pod", None)
            import time

            cutoff = None
            if since_secs:
                cutoff = time.time() - since_secs
            elif since_ts:
                cutoff = float(since_ts)
            shown = 0
            for h in items:
                hid = h.get("pod_name") or h.get("replica_id")
                if rep and str(hid) != str(rep):
                    continue
                if cutoff is not None:
                    try:
                        import datetime as _dt

                        t = _dt.datetime.fromisoformat(
                            h.get("check_time", "").replace("Z", "+00:00")
                        ).timestamp()
                        if t < float(cutoff):
                            continue
                    except Exception:
                        pass
                ts = h.get("check_time", "")
                print(
                    f"{ts} {hid}: ready={bool(h.get('ready'))} live={bool(h.get('live'))} "
                    f"R='{h.get('readiness_message') or ''}' L='{h.get('liveness_message') or ''}'"
                )
                shown += 1
                if shown >= limit:
                    break
            return 0
        except Exception as exc:  # noqa: BLE001
            print(f"remote history failed: {exc}")
            return 1
    # Local store fallback
    limit = int(getattr(args, "limit", 20))
    since_secs = (
        _parse_since_secs(getattr(args, "since", None)) if getattr(args, "since", None) else None
    )
    since_ts = (
        _parse_rfc3339_to_epoch(getattr(args, "since_time", None))
        if getattr(args, "since_time", None)
        else None
    )
    query_limit = max(limit, 200) if (since_secs or since_ts) else limit
    items = store.get_probe_history(app_name, query_limit)
    if getattr(args, "json", False):
        import json as _json
        import time

        def _filter(h):
            if since_secs is None and since_ts is None:
                return True
            try:
                cutoff = (time.time() - since_secs) if since_secs else float(since_ts)
                return h.check_time.timestamp() >= cutoff
            except Exception:
                return True

        j = [
            {
                "pod_name": h.pod_name,
                "replica_id": h.pod_name,
                "check_time": h.check_time.isoformat(),
                "ready": bool(h.ready),
                "live": bool(h.live),
                "readiness_message": h.readiness_message,
                "liveness_message": h.liveness_message,
            }
            for h in items
            if _filter(h)
        ][:limit]
        print(_json.dumps(j, indent=2))
        return 0
    rep = getattr(args, "pod", None)
    import time

    shown = 0
    for h in items:
        if rep and str(h.pod_name) != str(rep):
            continue
        if since_secs or since_ts:
            try:
                cutoff = (time.time() - since_secs) if since_secs else float(since_ts)
                if h.check_time.timestamp() < cutoff:
                    continue
            except Exception:
                pass
        ts = h.check_time.strftime("%Y-%m-%d %H:%M:%S")
        print(
            f"{ts} {h.pod_name}: ready={h.ready} live={h.live} R='{h.readiness_message}' L='{h.liveness_message}'"
        )
        shown += 1
        if shown >= limit:
            break
    return 0


def handle_volumes(args: argparse.Namespace, runtime: RuntimeAdapter) -> int:
    if args.vol_cmd == "list":
        try:
            app_filter = getattr(args, "app", None)
            if app_filter:
                app_filter = (
                    _resolve_app_name(app_filter, getattr(args, "namespace", None)) or app_filter
                )
            vols = runtime.list_storage_volumes(app_filter)  # type: ignore[attr-defined]
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
                print(
                    f"{name} driver={drv} mount={mnt} app={_display_app_name(app) if app else ''}"
                )
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

    app_name = _resolve_app_name(args.name, getattr(args, "namespace", None)) or args.name
    path = f"/logs/{app_name}"
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

    app_name = app_key_for_manifest(manifest)
    desired = int(manifest.spec.replicas)
    rollout = getattr(manifest.spec, "rollout", {}) or {}
    strategy = str(rollout.get("strategy", "parallel"))
    if not getattr(args, "json", False):
        print(f"Plan for {_display_app_name(app_key_for_manifest(manifest))}:")
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
                        "app": app_name,
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
                diagnostics["service"]["type"] = str(svc.type)
        except Exception:
            pass

    # Affinity warnings
    try:
        infos = runtime.list_containers_info()  # type: ignore[attr-defined]
    except Exception:
        infos = []
    running_same = [i for i in infos if (i.get("labels") or {}).get("ae.app") == app_name]
    if running_same and not getattr(args, "json", False):
        print(
            f"  - note: found {len(running_same)} running container(s) for app '{_display_app_name(app_name)}' (rollout surge may temporarily increase count)"
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
    running_same = [i for i in infos if (i.get("labels") or {}).get("ae.app") == app_name]
    if running_same:
        warnings.append(
            f'found {len(running_same)} running container(s) for app "{_display_app_name(app_name)}" (surge may increase count)'
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
                if "exec" in h and not isinstance(h.get("exec"), list | tuple):
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
                if "exec" in h and not isinstance(h.get("exec"), list | tuple):
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
            "app": app_name,
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

    # validate PDB values when provided (allow integer or percent string 0-100%)
    def _parse_pdb_value(v):
        if v is None:
            return None
        s = str(v).strip()
        if s == "":
            return None
        if s.endswith("%"):
            num = s[:-1]
            if num.isdigit():
                # keep as percentage string; optional bounds check 0-100
                try:
                    n = int(num)
                    if 0 <= n <= 100:
                        return f"{n}%"
                except Exception:
                    pass
            print("error: PDB percent must be an integer 0-100 followed by % (e.g., 50%)")
            return "__INVALID__"
        # integer form
        if s.isdigit():
            try:
                return int(s)
            except Exception:
                pass
        print("error: PDB value must be an integer or a percent like 50%")
        return "__INVALID__"

    _min_avail = _parse_pdb_value(getattr(args, "pdb_min_available", None))
    _max_unavail = _parse_pdb_value(getattr(args, "pdb_max_unavailable", None))
    if _min_avail == "__INVALID__" or _max_unavail == "__INVALID__":
        return 2
    opts = ExportOptions(
        workload_kind=str(getattr(args, "workload", "deployment")).title(),
        namespace=(str(args.namespace) if args.namespace else None),
        ingress_class_name=args.ingress_class,
        ingress_path_type=getattr(args, "ingress_path_type", None),
        ingress_annotations=(
            dict(
                a.split("=", 1) for a in (getattr(args, "ingress_annotation", []) or []) if "=" in a
            )
            if getattr(args, "ingress_annotation", None) is not None
            else None
        ),
        service_port=args.service_port,
        emit_configs=bool(getattr(args, "emit_configs", False)),
        inline_configs=bool(getattr(args, "inline_configs", False)),
        emit_secrets=bool(getattr(args, "emit_secrets", False)),
        inline_secrets=bool(getattr(args, "inline_secrets", False)),
        emit_storage=bool(getattr(args, "emit_storage", False)),
        default_pvc_size=str(getattr(args, "default_pvc_size", "1Gi")),
        storage_class_name=getattr(args, "storage_class_name", None),
        pvc_access_modes=(list(getattr(args, "pvc_access_modes", []) or []) or None),
        service_account_name=getattr(args, "service_account", None),
        emit_pdb=bool(getattr(args, "emit_pdb", False)),
        pdb_min_available=_min_avail,
        pdb_max_unavailable=_max_unavail,
        hpa_min=getattr(args, "hpa_min", None),
        hpa_max=getattr(args, "hpa_max", None),
        hpa_cpu_target=getattr(args, "hpa_cpu_target", None),
        hpa_mem_target=getattr(args, "hpa_mem_target", None),
        hpa_mem_type=getattr(args, "hpa_mem_type", None),
        hpa_mem_value=getattr(args, "hpa_mem_value", None),
        allow_hpa_without_requests=bool(getattr(args, "allow_hpa_no_requests", False)),
        default_security=bool(getattr(args, "default_security", False)),
        require_requests=bool(getattr(args, "require_requests", False)),
        hpa_behavior_up=(
            __import__("json").loads(args.hpa_behavior_up)
            if getattr(args, "hpa_behavior_up", None)
            else None
        ),
        hpa_behavior_down=(
            __import__("json").loads(args.hpa_behavior_down)
            if getattr(args, "hpa_behavior_down", None)
            else None
        ),
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
        emit_namespace=bool(getattr(args, "emit_namespace", False)),
        pod_security_enforce=getattr(args, "psa_enforce", None),
        job_backoff_limit=getattr(args, "job_backoff_limit", None),
        job_ttl_seconds_after_finished=getattr(args, "job_ttl_seconds_after_finished", None),
        cron_schedule=getattr(args, "cron_schedule", None),
        cron_concurrency_policy=getattr(args, "cron_concurrency_policy", None),
        cron_suspend=(True if bool(getattr(args, "cron_suspend", False)) else None),
        cron_starting_deadline_seconds=getattr(args, "cron_starting_deadline_seconds", None),
    )
    # Allow manifest exportHints to toggle certain options
    try:
        if getattr(man, "spec", None) and getattr(man.spec, "export_hints", None):
            if bool(getattr(man.spec.export_hints, "emit_pdb", False)):
                opts.emit_pdb = True
    except Exception:
        pass
    # Apply preset last so explicit flags take precedence
    if getattr(args, "preset", None):
        opts = apply_preset(opts, args.preset)  # type: ignore[arg-type]
    # Ingress preset after main preset and flags; still allow explicit --ingress-annotation to win
    if getattr(args, "ingress_preset", None):
        from ae.k8s.presets import apply_ingress_preset  # local import to avoid cycles

        opts = apply_ingress_preset(opts, args.ingress_preset)  # type: ignore[arg-type]
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
        import yaml as _yaml

        from ae.k8s.exporter import export_k8s_docs

        outdir: Path = args.split
        outdir.mkdir(parents=True, exist_ok=True)
        docs = export_k8s_docs(man, options=opts)
        for i, d in enumerate(docs, start=1):
            meta = d.get("metadata") or {}
            name = meta.get("name") or f"res-{i}"
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
            from ae.k8s.exporter import ExportOptions, export_k8s_yaml

            yaml_text = export_k8s_yaml(man, options=ExportOptions())
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
    import json as _json
    import shutil
    import subprocess as sp

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
        proc = sp.run(cmd, capture_output=True, text=True, check=False)  # noqa: S603,S607 - cosign path vetted via shutil.which; shell disabled
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


def handle_certs(args: argparse.Namespace) -> int:
    import json as _json
    import os
    from pathlib import Path

    from ae.security import is_revoked

    root = Path(args.root) if args.root else Path(os.getenv("AE_TLS_DIR", "state/tls"))
    issued_path = root / "issued.json"
    revoked_path = root / "revoked.json"
    issued = []
    revoked = []
    try:
        if issued_path.exists():
            issued = _json.loads(issued_path.read_text())
    except Exception as exc:  # noqa: BLE001
        print(f"failed to read {issued_path}: {exc}")
    try:
        if revoked_path.exists():
            revoked = _json.loads(revoked_path.read_text())
    except Exception:
        revoked = []
    if getattr(args, "json", False):
        print(_json.dumps({"root": str(root), "issued": issued, "revoked": revoked}, indent=2))
        return 0
    print(f"TLS dir: {root}")
    print("Issued certs:")
    if not issued:
        print("  (none)")
    else:
        for item in issued:
            nid = item.get("node_id")
            serial = item.get("serial", "")
            exp = item.get("expires_at", "")
            status = "revoked" if (is_revoked and serial and is_revoked(serial, root=root)) else ""
            print(f"  node={nid} serial={serial} expires={exp} {status}")
    if revoked:
        print("Revoked serials:")
        for s in revoked:
            print(f"  {s}")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
