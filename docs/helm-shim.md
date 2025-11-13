# Helm + k1s API Shim Runbook

Use this playbook to prove a chart install end-to-end against the shim. It assumes a stub runtime (no real containers) but exercises the Kubernetes API surface, including RBAC, PDB, HPA, Services, Ingress, and CRDs.

## 1. Start the shim

```bash
PYTHONPATH=src AE_APISHIM_RUNTIME=stub \
  python -m ae.apishim serve --host 127.0.0.1 --port 8445 --token devtoken
```

> Tip: add `--tls` plus `AE_APISHIM_TLS_{CERT,KEY}` for HTTPS. For dev, HTTP + token is fine.

## 2. Write a kubeconfig stanza

```bash
PYTHONPATH=src python -m ae.apishim kubeconfig \
  --server http://127.0.0.1:8445 --token devtoken \
  --context k1s-shim --insecure-skip-tls-verify > ~/.kube/k1s-shim
export KUBECONFIG=~/.kube/k1s-shim
```

## 3. Create a sample chart and tweak values

```bash
helm create demochart
cat <<'YAML' > demochart/values.yaml
replicaCount: 1
image:
  repository: nginx
  tag: "1.27"
service:
  type: NodePort
  # nodePort optional; shim auto-allocates from AE_APISHIM_NODEPORT_{MIN,MAX}
  nodePort: 31080
resources: {}
ingress:
  enabled: true
  className: ""
  hosts:
    - host: demo.local
      paths:
        - path: /
          pathType: Prefix
  tls: []
YAML
```

## 4. Install and check status

```bash
kubectl create namespace demo
helm install demo demochart -n demo --wait
kubectl -n demo get deploy,svc,ing
```

What happens behind the scenes:

- The shim stores Deployments/Services/Ingress and maps them to a k1s `App` manifest.
- `Service.type=NodePort` makes the runtime bind a host port. If you omit `nodePort`, the shim auto-allocates from `AE_APISHIM_NODEPORT_MIN/MAX` and reuses it across restarts, so `curl http://127.0.0.1:<assigned>` reaches the pod.
- Ingress rules translate to a Caddy snippet (when k1s ingress is enabled) so `demo.local` resolves to the NodePort.

## 5. Upgrade, patch, uninstall

```bash
helm upgrade demo demochart -n demo --set replicaCount=2
helm history demo -n demo
helm uninstall demo -n demo
```

## Supported features (Nov 2025)

| Resource | CRUD | Watch | Notes |
| --- | --- | --- | --- |
| Namespace, ConfigMap, Secret, ServiceAccount | ✅ | ✅ | |
| Service (NodePort/ClusterIP) | ✅ | ✅ | NodePort auto-publishes host ports |
| Deployment (+/status +/scale) | ✅ | ✅ | Wired to k1s runtime |
| Ingress (networking.k8s.io/v1) | ✅ | ✅ | First host/path per service applied |
| RBAC (Role/RoleBinding/ClusterRole/ClusterRoleBinding) | ✅ | ✅ | stored, no authz enforcement |
| PodDisruptionBudget | ✅ | ✅ | stored |
| HorizontalPodAutoscaler (autoscaling/v2) | ✅ | ✅ | stored |
| CustomResourceDefinition + custom resources | ✅ | ✅ | dynamic discovery |

Limitations:

- CRDs install, but controllers/webhooks from charts are not run.
- Services only expose host ports when `type` is `NodePort`/`LoadBalancer` with `nodePort` set.
- Ingress support covers host/path backends; advanced annotations are ignored.
- Runtime defaults to `stub`. Set `AE_APISHIM_RUNTIME=podman` (or docker) to run real containers.

## One-command demo

Prefer automation? Run the helper script (or Make target) to exercise the workflow end-to-end:

```bash
make shim-helm-demo
# or
PORT=8450 TOKEN=demo RUNTIME=podman bash scripts/helm_shim_demo.sh
```

The script starts the shim, generates a kubeconfig, scaffolds a chart, installs/inspects/uninstalls it, and tears everything down. Use environment variables (`PORT`, `TOKEN`, `RUNTIME`, `CHART_NAME`, `NAMESPACE`) to customize the run.
```
