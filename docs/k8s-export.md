Kubernetes Export Guide

This project can export your `ae.dev/v1alpha1` App manifests to upstream Kubernetes YAML using a conservative, portable subset of the API. Use `ae export-k8s` or `python -m ae.cli export-k8s` to render manifests, and `ae k8s-check` to self-audit portability.

Basics
- Workloads: `Deployment` (default) or `StatefulSet` via `--workload statefulset`.
- Networking: emits `Service` and `Ingress` v1 when the manifest has ports/ingress.
- Optional resources: ConfigMap/Secret, PDB, HPA, ServiceAccount, NetworkPolicy.

Quick start
- Export echo example to YAML and validate basic structure:
  - `python -m ae.cli export-k8s -f specs/examples/echo.yaml --namespace demo --ingress-class traefik --validate`
- Split output into per-resource files:
  - `python -m ae.cli export-k8s -f specs/examples/echo.yaml --namespace demo --split out/`
- Optional schema validation with kubeconform:
  - `python -m ae.cli k8s-check -f specs/examples/echo.yaml --kubeconform --emit`
  - Or run kubeconform directly: `kubeconform -strict -summary < k8s.yaml`

What’s supported
- Probes: readiness, liveness, startup → maps to `readinessProbe`, `livenessProbe`, `startupProbe`.
- Image pulls: `spec.imagePullPolicy` and `spec.imagePullSecrets: [name, ...]`.
  - Generate a Secret from your local registry creds: `ae registry kubesecret --name regcred --namespace demo -o regcred.yaml`, then reference with `spec.imagePullSecrets: [regcred]`.
- env and envFrom:
  - Explicit `spec.env: [{name,value}]`.
  - Key refs from `configRefs[].env[]`/`secretRefs[].env[]` → `env[*].valueFrom`.
  - Set `configRefs[].envFrom: true` or `secretRefs[].envFrom: true` to emit `envFrom` entries.
  - Field refs: `env[].valueFrom.fieldRef.fieldPath` and `resourceFieldRef` are passed through (e.g., `metadata.name`, `resources.requests.cpu`). Locally, a minimal subset is resolved (metadata.name/namespace); other fields are export-only.
- File projections → projected volume:
  - `configRefs[].files[]` and `secretRefs[].files[]` create a single projected volume mounted at `/var/run/ae/config` with per-key `items.path`.
- Pod security
  - Container-level: `security.{runAsUser,runAsGroup,readOnlyRootFilesystem,dropCapabilities,seccomp*}`.
  - Pod-level: `podSecurity.fsGroup` and `podSecurity.seccompProfile*` → `pod.spec.securityContext`.
 - Pod DNS and identity
  - `dnsPolicy` and `dnsConfig { nameservers, searches, options }` pass through to the PodSpec.
  - `hostname` and `subdomain` pass through to the PodSpec (useful with StatefulSets + headless Services).
  - `hostAliases`, `enableServiceLinks`, `shareProcessNamespace`, `hostNetwork`, `nodeSelector`, and `setHostnameAsFQDN` pass through when set.
 - Lifecycle
   - Export: `lifecycle.postStart|preStop` with `exec|httpGet|tcpSocket` handlers.
   - Runtime: preStop is executed best‑effort when removing old replicas. Exec handlers run inside the container; HTTP/TCP handlers are fired against the first published host port. You can set a runtime‑only timeout via `lifecycle.preStop.timeoutSeconds` (defaults to `terminationGracePeriodSeconds`).
- Services: multi-port mapping via `spec.service.ports[]` with NodePort validation; externalIPs passthrough.
- Ingress: `networking.k8s.io/v1`, `PathType=Prefix`, optional `--ingress-class` and TLS `tlsSecretName`.
- PDB/HPA: use flags to enable; HPA guards require requests unless overridden.

Examples
- Emit ConfigMap/Secret objects inline and envFrom + file projections:
  - `python -m ae.cli export-k8s -f specs/examples/envfrom-and-projection.yaml --emit-configs --emit-secrets --namespace demo --validate > out.yaml`

Flags you’ll likely use
- `--namespace demo` set metadata.namespace for all objects.
- `--workload statefulset` for stateful apps (adds headless Service and volumeClaimTemplates when `--emit-storage`).
- `--emit-configs --emit-secrets` to include ConfigMap/Secret objects.
- `--emit-storage` to emit PVCs (Deployment) or use `volumeClaimTemplates` (StatefulSet).
- `--service-account app-sa`, `--emit-pdb`, `--pdb-{min-available|max-unavailable}`.
- `--hpa-min 2 --hpa-max 5 --hpa-cpu-target 70` (or memory targets; see `ae k8s-check --help`).
- `--preset` presets: web-basic | web-hardened | scale-ready | web-strict.

QoS tip
- For predictable scheduling/QoS, set both `resources.requests` and `resources.limits`. The `k8s-check` tool warns when limits are set without requests.

Portability checks
- `python -m ae.cli k8s-check -f specs/examples/echo.yaml --policy strict` warns about missing requests/probes and recommends startupProbe when liveness exists.

Notes
- The exporter emits only stable API groups: apps/v1, v1, networking.k8s.io/v1, policy/v1, autoscaling/v2.
- The local runtime diverges from Kubernetes networking; the exporter’s YAML is intended for upstream clusters.
