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

    # NetworkPolicy advisories: recommend a default deny unless explicitly set
    if not getattr(spec, "network_policy", None):
        issues.append(
            Issue(
                "warn",
                "NP_RECOMMENDED_DEFAULT_DENY",
                "no NetworkPolicy set; consider a default-deny with explicit allowances (use export flags)",
            )
        )

    # Security: prefer non-root and read-only root fs.
    if not spec.security:
        issues.append(
            Issue(
                "warn",
                "SEC_NO_CONTEXT",
                "no security spec; consider runAsUser, readOnlyRootFilesystem",
            )
        )
    else:
        if spec.security.run_as_user is None:
            issues.append(
                Issue("warn", "SEC_UID", "security.runAsUser not set; prefer non-root (e.g., 1000)")
            )
        if not spec.security.read_only_root:
            issues.append(
                Issue(
                    "warn",
                    "SEC_RO_ROOT",
                    "security.readOnlyRootFilesystem is false; enable if possible",
                )
            )

    # Probes and graceful shutdown
    if not spec.health or not spec.health.readiness:
        issues.append(Issue("warn", "PROBE_READINESS", "no readiness probe configured"))
    if not spec.health or not spec.health.liveness:
        issues.append(Issue("warn", "PROBE_LIVENESS", "no liveness probe configured"))
    # Recommend startup probe when liveness exists (helps cold starts)
    if spec.health and spec.health.liveness and not getattr(spec.health, "startup", None):
        issues.append(
            Issue(
                "warn",
                "PROBE_STARTUP_RECOMMENDED",
                "liveness configured without startup probe; consider startupProbe for slow starts",
            )
        )

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
    # emptyDir advisories: ephemeral storage warning
    if getattr(spec, "empty_dirs", None):
        for ed in spec.empty_dirs:
            try:
                mnt = (
                    getattr(ed, "mount_path", None) or ed.get("mountPath")
                    if isinstance(ed, dict)
                    else None
                )
            except Exception:
                mnt = None
            issues.append(
                Issue(
                    "warn",
                    "EMPTYDIR_EPHEMERAL",
                    f"emptyDir volume at {mnt or '/path'} is ephemeral; data is lost on Pod restart/eviction",
                )
            )
    # Multi-replica without PDB (skip when manifest requests exporter to emit a PDB)
    if int(spec.replicas) > 1:
        has_pdb_hint = False
        try:
            if getattr(spec, "export_hints", None) and bool(
                getattr(spec.export_hints, "emit_pdb", False)
            ):
                has_pdb_hint = True
        except Exception:
            has_pdb_hint = False
        if not has_pdb_hint:
            issues.append(
                Issue(
                    "warn",
                    "PDB_MISSING",
                    "multi-replica app without PodDisruptionBudget (recommend minAvailable: 1)",
                )
            )
        # Recommend anti-affinity or topology spread for multi-replica resilience
        has_affinity = bool(getattr(spec, "affinity", None))
        has_spread = bool(getattr(spec, "topology_spread_constraints", None))
        if not (has_affinity or has_spread):
            issues.append(
                Issue(
                    "warn",
                    "ANTI_AFFINITY_RECOMMENDED",
                    "multi-replica app without anti-affinity or topology spread; consider podAntiAffinity or topologySpreadConstraints",
                )
            )
        # HPA pre-reqs advisory
        req_cpu = bool(
            getattr(spec.resources, "requests", None)
            and getattr(spec.resources.requests, "cpu", None)
        )
        if not req_cpu:
            issues.append(
                Issue(
                    "warn",
                    "HPA_PREREQ_REQUESTS",
                    "HPA requires container resources.requests.cpu; set it before enabling HPA",
                )
            )
    else:
        # Canary with single replica is a no-op; warn
        rollout = getattr(spec, "rollout", {}) or {}
        if str(rollout.get("strategy", "")).lower() == "canary":
            issues.append(
                Issue(
                    "warn",
                    "CANARY_SINGLE_REPLICA",
                    "canary strategy with replicas=1 has no effect; set replicas>1",
                )
            )

    # Image arch: cannot verify; warn as informational only unless suppressed by exportHints
    suppress_img = False
    try:
        if getattr(spec, "export_hints", None) and bool(
            getattr(spec.export_hints, "suppress_image_multi_arch_warning", False)
        ):
            suppress_img = True
    except Exception:
        suppress_img = False
    if not suppress_img:
        issues.append(
            Issue(
                "warn",
                "IMAGE_MULTI_ARCH_UNKNOWN",
                "cannot verify image is multi-arch (amd64/arm64)",
            )
        )
    # Multi-container advisory
    if getattr(spec, "containers", None):
        try:
            if len(list(spec.containers)) > 1:
                issues.append(
                    Issue(
                        "warn",
                        "RUNTIME_SINGLE_CONTAINER",
                        "export supports multiple containers, but local runtime runs one container",
                    )
                )
        except Exception:
            pass

    # Lifecycle advisories
    if getattr(spec, "lifecycle", None) and getattr(spec.lifecycle, "pre_stop", None) is not None:
        # If preStop exists but grace period is very small, warn about possible truncation
        try:
            tgp = int(getattr(spec, "termination_grace_period_seconds", 10) or 10)
        except Exception:
            tgp = 10
        if tgp < 2:
            issues.append(
                Issue(
                    "warn",
                    "PRESTOP_SHORT_GRACE",
                    "preStop defined but terminationGracePeriodSeconds < 2s; increase to allow hook to run",
                )
            )

    # QoS: limits without requests leads to Burstable with risk of throttling; recommend setting requests
    res = getattr(spec, "resources", None)
    if res and getattr(res, "limits", None) and not getattr(res, "requests", None):
        issues.append(
            Issue(
                "warn",
                "QOS_LIMITS_NO_REQUESTS",
                "resources.limits set without resources.requests; define requests for predictable QoS",
            )
        )

    return issues


def apply_policy(issues: List[Issue], policy: str) -> List[Issue]:
    """Transform issues based on a named policy (baseline|strict)."""
    if policy.lower() != "strict":
        return issues
    out: List[Issue] = []
    # Escalate only readiness probe and resource requests; keep PDB advisory as a warning
    # because PDB is an export-time policy that may be satisfied by exporter flags/presets.
    escalate = {
        "PROBE_READINESS",
        "REQS_NONE",
        "HPA_CPU_REQUESTS_MISSING",
        "HPA_MEM_REQUESTS_MISSING",
    }
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
            issues.append(
                Issue(
                    "warn",
                    "HPA_CPU_REQUESTS_MISSING",
                    "HPA CPU utilization requires resources.requests.cpu",
                )
            )
        elif a == "mem-util" and not mem_req:
            issues.append(
                Issue(
                    "warn",
                    "HPA_MEM_REQUESTS_MISSING",
                    "HPA memory utilization requires resources.requests.memory",
                )
            )
        elif a.startswith("mem-value="):
            q = a.split("=", 1)[1].strip()
            if not _valid_quantity(q):
                issues.append(
                    Issue(
                        "error",
                        "HPA_MEM_VALUE_INVALID",
                        f"invalid memory quantity for AverageValue: {q}",
                    )
                )
    return issues


def _valid_quantity(q: str) -> bool:
    import re

    # Accept integers with optional binary SI suffix (Ki, Mi, Gi, Ti, Pi, Ei)
    # and decimal SI (K, M, G, T, P, E). Keep validation pragmatic.
    pattern = re.compile(r"^\d+(?:\.(?:\d+))?\s*(?:[KMGTP]i?|)$", re.IGNORECASE)
    return bool(pattern.match(q))
    # Ingress validations: host/path sanity
    if spec.ingress:
        host = str(getattr(spec.ingress, "host", "") or "").strip()
        path = str(getattr(spec.ingress, "path", "") or "/").strip()
        if not host:
            issues.append(Issue("error", "INGRESS_HOST_EMPTY", "ingress.host must be set"))
        else:
            import re as _re

            # Basic hostname check: labels separated by dots, allow localhost for dev
            if host != "localhost" and not _re.match(
                r"^(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,}$", host
            ):
                issues.append(
                    Issue("warn", "INGRESS_HOST_FORMAT", f"ingress.host '{host}' looks unusual")
                )
        if not path.startswith("/"):
            issues.append(Issue("error", "INGRESS_PATH_FORMAT", "ingress.path must start with '/'"))

    # Service validations
    if getattr(spec, "service", None) and getattr(spec.service, "ports", None):
        names = []
        nums = []
        for sp in spec.service.ports:
            n = getattr(sp, "name", None)
            p = getattr(sp, "port", None)
            if n:
                if n in names:
                    issues.append(
                        Issue("error", "SVC_PORT_NAME_DUP", f"duplicate Service port name: {n}")
                    )
                names.append(n)
            if p is not None:
                if p in nums:
                    issues.append(
                        Issue("warn", "SVC_PORT_NUM_DUP", f"duplicate Service port number: {p}")
                    )
                nums.append(p)
            # targetPort match check (numeric)
            tp = getattr(sp, "target_port", None)
            if tp is not None:
                try:
                    tpi = int(tp)
                except Exception:
                    tpi = None
                if tpi is not None:
                    if not any(
                        int(getattr(cp, "container_port", 0)) == tpi for cp in spec.ports or []
                    ):
                        issues.append(
                            Issue(
                                "warn",
                                "SVC_TARGETPORT_MISSING",
                                f"targetPort {tpi} not found in container ports",
                            )
                        )


# ruff: noqa
# ruff: noqa: E501,UP006,UP007,UP017,UP035,S110,S112,SIM102,SIM105,SIM108,F821
