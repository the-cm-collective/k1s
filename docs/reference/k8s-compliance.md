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

## Current Coverage Summary (2026-01-16)

- Workloads: Deployment full support; StatefulSet/DaemonSet/Job/CronJob are stored with best-effort status but emulated as Deployment-like apps (no real completion, scheduling, or per-node placement).
- Pod/Container: env/envFrom; readiness/liveness/startup probes; lifecycle hooks; resources requests/limits; securityContext (runAs*/fsGroup/readOnlyRootFilesystem/cap drop/seccomp/AppArmor); terminationGracePeriodSeconds; priorityClassName.
- Service: ClusterIP/NodePort/LoadBalancer with multi-port mapping, nodePort validation, externalIPs, sessionAffinity; EndpointSlice projection with topology hints; service port-forward supported by shim.
- Ingress: networking.k8s.io/v1 Prefix paths with TLS and ingressClassName; status.loadBalancer populated from Service VIP/provider IPs.
- Policy/Autoscaling/Accounts: PDB (int/percent), HPA v2 (CPU/memory utilization/averageValue), ServiceAccount tokens, Role/RoleBinding emit/enforce in shim, NetworkPolicy passthrough.
- Scheduling: affinity/tolerations/topologySpreadConstraints/nodeSelector/priorityClassName passed to Pod templates; scheduler in k1s honors selectors/tolerations/topology spread and storage pinning.
- Runtime/Networking: Service CIDR + overlay provider with ClusterIP allocation, EndpointSlice projection, port-forward for pods/services, exec/logs via agents.
- Discovery/Tooling: OpenAPI v2/v3 drift guard + fixtures, kubectl/helm apply/watch/port-forward gates in CI, compatibility matrix (`apishim-compatibility-matrix.html`).

## Parity Checklist

- Use readiness/liveness/startup probes.
- Provide `resources.requests/limits`; enable `--policy strict` in `ae k8s-check`.
- Add topology spread or anti-affinity for multi-replica apps.
- Keep retained volumes single-node or use PVCs when exporting to Kubernetes.
- Prefer Service VIP + ingress over hostPorts for HA; reserve hostPorts for single-node edges.

## Verification Notes

- Exporter validated with `ae export-k8s --validate` + `ae k8s-check --policy strict`.
- Shim gates: kubectl/helm apply/get/watch/port-forward exercised in CI with OpenAPI drift guard.
- Multi-node overlay path covered by integration tests and the multi-node lab guide.

### Notable Gaps vs. Kubernetes

- NetworkPolicy enforcement depends on your CNI when exporting; k1s runtime does not enforce policies.
- PodSecurityAdmission/admission webhooks are not implemented; exporter can emit PSA labels only.
- PV/PVC/StorageClass/CSI provisioning beyond exported manifests; k1s runtime relies on retained named volumes.
- metrics.k8s.io/metrics-server and aggregated APIs are out of scope.
- Advanced Ingress features (regex, weighted/canary annotations, multiple backends per rule) remain out of scope for now.

## Demo Manifests

- `specs/examples/echo-sec.yaml`: non-root + read-only root filesystem + HTTP readiness + ingress.
  - Apply: `python -m ae.cli apply -f specs/examples/echo-sec.yaml`
- `specs/examples/echo-tcp.yaml`: TCP readiness with thresholds + ingress.
  - Apply: `python -m ae.cli apply -f specs/examples/echo-tcp.yaml`
- `specs/examples/echo-exec.yaml`: Exec readiness probe + ingress.
  - Apply: `python -m ae.cli apply -f specs/examples/echo-exec.yaml`

## Remote Apply

- Enable mutations and token, then:
  - `ae --server https://api.home.arpa:8443 --token <admin> apply -f specs/examples/echo-sec.yaml`

## NetworkPolicy Provider Notes (k3s)

- Enforcement depends on your CNI plugin. The default k3s installation uses flannel, which does not enforce NetworkPolicy. In that setup, policies are created but not applied by the dataplane.
- To enforce policies on k3s, choose a CNI that implements NetworkPolicy (for example, Calico or Cilium). Install it per vendor instructions and disable flannel when required by that CNI.
- Use the exporter presets to bootstrap sane defaults:
  - Web tier: `--emit-np --np-preset web` (default deny + web/DNS egress allowed).
  - Backend tier: `--emit-np --np-preset backend` (default deny + DNS egress and RFC1918 egress to common DB/cache ports).

### Gap Tracker (create issues and check off when done)

- [x] Add `startupProbe` to spec and exporter; update `k8s-check` guidance.
- [x] Support `envFrom` for ConfigMap/Secret; exporter maps to envFrom.
- [x] Support `imagePullSecrets` and `imagePullPolicy` in spec/exporter.
- [x] Allow PDB percentage values and validate exclusivity with integers.
- [ ] Add HPA scaleUp/scaleDown behavior knobs (stabilizationWindow, policies).
- [ ] Model Config/Secret volume mounts and mountPaths; exporter emits volumes/volumeMounts.
- [ ] PVC `storageClassName` and `accessModes` selection flags; document defaults.
- [ ] Service healthCheckNodePort and advanced Ingress annotations (behind explicit flag).
- [ ] RBAC: broaden exporter coverage for ClusterRole/ClusterRoleBinding presets.
