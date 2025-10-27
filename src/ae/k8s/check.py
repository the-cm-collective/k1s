"""K8s portability checker for AppManifest.

Implements the "Quick self-check checklist" from FEAT.md with pragmatic
heuristics. Emits warnings and errors and can be used by the CLI with
--strict to fail on errors.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List

from ae.controller.spec import AppManifest


@dataclass(slots=True)
class Issue:
    level: str  # "warn" or "error"
    code: str
    message: str


def k8s_portability_issues(m: AppManifest) -> List[Issue]:
    issues: List[Issue] = []
    spec = m.spec

    # Stable APIs: our exporter only uses stable APIs, so no check here.

    # ClusterIP + Ingress: If ingress is set but there are no ports, flag error.
    if spec.ingress and not spec.ports:
        issues.append(
            Issue(
                "error",
                "INGRESS_NO_PORTS",
                "ingress defined but no container ports; export requires a Service target",
            )
        )

    # Controller/cloud-specific annotations: not applicable at manifest level; skip.

    # NetworkPolicy reliance: N/A at manifest level; assume safe.

    # Security: prefer non-root and read-only root fs.
    if not spec.security:
        issues.append(Issue("warn", "SEC_NO_CONTEXT", "no security spec; consider runAsUser, readOnlyRootFilesystem"))
    else:
        if spec.security.run_as_user is None:
            issues.append(Issue("warn", "SEC_UID", "security.runAsUser not set; prefer non-root (e.g., 1000)"))
        if not spec.security.read_only_root:
            issues.append(Issue("warn", "SEC_RO_ROOT", "security.readOnlyRootFilesystem is false; enable if possible"))

    # Probes and graceful shutdown
    if not spec.health or not spec.health.readiness:
        issues.append(Issue("warn", "PROBE_READINESS", "no readiness probe configured"))
    if not spec.health or not spec.health.liveness:
        issues.append(Issue("warn", "PROBE_LIVENESS", "no liveness probe configured"))

    # CPU/Memory requests
    if not spec.resources or not spec.resources.requests:
        issues.append(Issue("warn", "REQS_NONE", "no resources.requests defined (cpu/memory)"))

    # PVC portability: our manifest `storage` is PV-lite; fine. HostPath volumes warn.
    if getattr(spec, "volumes", None):
        for v in spec.volumes:
            if not getattr(v, "read_only", True):
                issues.append(
                    Issue(
                        "warn",
                        "HOSTPATH_RW",
                        f"hostPath at {v.mount_path} is RW; prefer portable storage or read-only binds",
                    )
                )
    # Multi-replica without PDB
    if int(spec.replicas) > 1:
        issues.append(Issue("warn", "PDB_MISSING", "multi-replica app without PodDisruptionBudget (recommend minAvailable: 1)"))
        # HPA pre-reqs advisory
        req_cpu = bool(getattr(spec.resources, "requests", None) and getattr(spec.resources.requests, "cpu", None))
        if not req_cpu:
            issues.append(Issue("warn", "HPA_PREREQ_REQUESTS", "HPA requires container resources.requests.cpu; set it before enabling HPA"))
    else:
        # Canary with single replica is a no-op; warn
        rollout = getattr(spec, "rollout", {}) or {}
        if str(rollout.get("strategy", "")).lower() == "canary":
            issues.append(Issue("warn", "CANARY_SINGLE_REPLICA", "canary strategy with replicas=1 has no effect; set replicas>1"))

    # Image arch: cannot verify; warn as informational only.
    issues.append(Issue("warn", "IMAGE_MULTI_ARCH_UNKNOWN", "cannot verify image is multi-arch (amd64/arm64)"))

    return issues


def apply_policy(issues: List[Issue], policy: str) -> List[Issue]:
    """Transform issues based on a named policy (baseline|strict)."""
    if policy.lower() != "strict":
        return issues
    out: List[Issue] = []
    escalate = {"PROBE_READINESS", "REQS_NONE", "PDB_MISSING"}
    for it in issues:
        if it.code in escalate and it.level == "warn":
            out.append(Issue("error", it.code, it.message))
        else:
            out.append(it)
    return out


def infer_hpa_issues(manifest, assumptions: list[str]) -> List[Issue]:  # noqa: ANN001
    """Validate HPA assumptions against the manifest's resources.

    Supported assumptions:
    - cpu-util
    - mem-util
    - mem-value=<quantity>  (e.g., 200Mi)
    """
    issues: List[Issue] = []
    spec = manifest.spec
    res = getattr(spec, "resources", None)
    req = getattr(res, "requests", None) if res else None
    cpu_req = bool(req and getattr(req, "cpu", None))
    mem_req = bool(req and getattr(req, "memory", None))

    for a in assumptions:
        a = str(a).strip()
        if a == "cpu-util" and not cpu_req:
            issues.append(Issue("warn", "HPA_CPU_REQUESTS_MISSING", "HPA CPU utilization requires resources.requests.cpu"))
        elif a == "mem-util" and not mem_req:
            issues.append(Issue("warn", "HPA_MEM_REQUESTS_MISSING", "HPA memory utilization requires resources.requests.memory"))
        elif a.startswith("mem-value="):
            q = a.split("=", 1)[1].strip()
            if not _valid_quantity(q):
                issues.append(Issue("error", "HPA_MEM_VALUE_INVALID", f"invalid memory quantity for AverageValue: {q}"))
    return issues


def _valid_quantity(q: str) -> bool:
    import re

    # Accept integers with optional binary SI suffix (Ki, Mi, Gi, Ti, Pi, Ei)
    # and decimal SI (K, M, G, T, P, E). Keep validation pragmatic.
    pattern = re.compile(r"^\d+(?:\.(?:\d+))?\s*(?:[KMGTP]i?|)$", re.IGNORECASE)
    return bool(pattern.match(q))
