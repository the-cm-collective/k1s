# Kubernetes Compliance

This page summarizes our current Kubernetes spec compliance for exported manifests.

How it works
- We export K8s YAML from representative App manifests using `ae cli export-k8s` (preset: web-hardened).
- We run offline structural validation, optional `kubeconform -strict` schema checks, optional `kubectl apply --dry-run=server`, and our `k8s-check --policy strict`.
- A weighted score is computed per sample; the overall score is the average across samples.

Update the status
- Generate a fresh report and write it where the docs server picks it up:
  - `python -m ae.cli k8s-report --run-dry-run -o docs/site/k8s_status.json`
- Rebuild docs to embed the status in this page:
  - `python docs/build_docs.py`

Online (cluster-backed) checks
- If you have kubectl and a cluster (Kind or k3s via k3d):
  - `python -m ae.cli k8s-report --run-dry-run --apply-online --cleanup -o docs/site/k8s_status.json`
- This adds server-side dry-run and applies the exported YAML to the cluster, waiting for the Deployment rollout; results are included in the score.

The compliance status and per-sample details render below when a report is present.

## Current Coverage Summary (2025-11-12)

- Workloads: Deployment (apps/v1), StatefulSet, Job, and CronJob supported; DaemonSet not emitted.
- Pod/Container: env pairs; env via configMapKeyRef/secretKeyRef; readiness/liveness (HTTP/TCP/exec); resources requests/limits; securityContext (runAsUser/runAsGroup/readOnlyRootFilesystem/cap drop/seccomp); AppArmor via annotation; terminationGracePeriodSeconds; priorityClassName.
- Service: ClusterIP with multi-port mapping; NodePort/LoadBalancer honored; nodePort validated (30000–32767); externalIPs passthrough.
- Ingress: networking.k8s.io/v1 Prefix paths; multi-path; TLS default on with optional secretName; ingressClassName optional.
- Policy/Autoscaling/Accounts: PDB (policy/v1; integers or percent), HPA (autoscaling/v2 CPU/memory utilization or memory AverageValue), ServiceAccount attach/emit, Role/RoleBinding emit when a ServiceAccount is attached, NetworkPolicy passthrough.
- Scheduling: affinity/tolerations/topologySpreadConstraints/priorityClassName passed to Pod template.
- Validation/Tooling: `ae export-k8s --validate`, `ae k8s-check --policy strict`, `ae k8s-report` (kubeconform + optional kubectl dry-run/apply).

### Notable Gaps vs. Kubernetes

- StartupProbe and lifecycle hooks (postStart/preStop) not modeled.
- `envFrom`, `imagePullSecrets`, and `imagePullPolicy` not modeled.
 - HPA advanced behaviors (scale policies) not supported.
- Service sessionAffinity/healthCheckNodePort and advanced Ingress annotations not emitted.
- Config/Secret volume mounts not modeled (env/key refs only); PVC `storageClassName` and accessModes are fixed.
 - RBAC: namespaced Role + RoleBinding emitted when a ServiceAccount is attached. ClusterRole/ClusterRoleBinding remain out of scope for now.

## NetworkPolicy Provider Notes (k3s)

- Enforcement depends on your CNI plugin. The default k3s installation uses flannel, which does not enforce NetworkPolicy. In that setup, policies are created but not applied by the dataplane.
- To enforce policies on k3s, choose a CNI that implements NetworkPolicy (for example, Calico or Cilium). Install it per vendor instructions and disable flannel when required by that CNI.
- Use the exporter presets to bootstrap sane defaults:
  - Web tier: `--emit-np --np-preset web` (default deny + web/DNS egress allowed).
  - Backend tier: `--emit-np --np-preset backend` (default deny + DNS egress and RFC1918 egress to common DB/cache ports).

### Gap Tracker (create issues and check off when done)

- [ ] Add `startupProbe` to spec and exporter; update `k8s-check` guidance.
- [ ] Support `envFrom` for ConfigMap/Secret; exporter maps to envFrom.
- [ ] Support `imagePullSecrets` and `imagePullPolicy` in spec/exporter.
- [x] Allow PDB percentage values and validate exclusivity with integers. (2025-11-12)
- [ ] Add HPA scaleUp/scaleDown behavior knobs (stabilizationWindow, policies).
- [ ] Model Config/Secret volume mounts and mountPaths; exporter emits volumes/volumeMounts.
- [ ] PVC `storageClassName` and `accessModes` selection flags; document defaults.
- [ ] Service sessionAffinity and healthCheckNodePort (behind explicit flag).
- [x] Optional RBAC emission for a ServiceAccount (Role/RoleBinding presets). (2025-11-12)
