# ClusterIP Compatibility and Multi-Node Path

This document outlines a pragmatic, phased plan to add a Kubernetes‑like ClusterIP Service abstraction to ae/k1s, starting with single‑node compatibility and evolving to multi‑node support. It’s designed to be implemented incrementally with minimal churn to existing components.

---

## Goals

- Provide a stable, in‑cluster virtual IP per Service (ClusterIP‑like) for app‑to‑app and ingress routing.
- Decouple clients from ephemeral replica endpoints; only “ready” backends receive traffic.
- Keep Phase 1 single‑node, Docker‑only; enable a clean path to multi‑node later.
- Avoid breaking existing manifests and CLI flows.

## Non‑Goals (Initial)

- Full Kubernetes Service and DNS semantics (headless/endpointslices). 
- NodePort/LoadBalancer at the runtime level (still via exporter → real K8s).
- In‑kernel kube‑proxy parity on day one.

---

## Architecture Overview

- Single shared bridge network (e.g., `ae-net`) where all app containers and per‑Service proxies live.
- One lightweight proxy container per Service (HAProxy/Envoy) with a static IP allocated from a Service CIDR.
- Controller owns IP allocation and proxy config driven by readiness; Ingress points to the Service IPs.
- A provider interface abstracts the dataplane so we can swap Docker bridge for iptables/IPVS/Envoy‑per‑node later.

---

## Phase 1 — Single Node ClusterIP Emulation

Deliverables
- Create/ensure a Docker bridge network with IPAM (env‑configurable name/CIDR).
- Per‑Service proxy container with a static IP (ClusterIP) on that network.
- Controller updates proxy backends to “ready” replica IP:port pairs.
- Ingress switches from per‑replica upstreams to Service IP:port.
- Persist Service IPs and endpoints in SQLite.

Key Changes
- Runtime network
  - Ensure network creation and connect app containers: `src/ae/runtime/docker_runtime.py`.
  - Use `AE_NETWORK_NAME`/`AE_NETWORK_SUBNET` to control the bridge and subnet.
- Service proxy
  - New module: `src/ae/network/service_proxy.py` with functions:
    - `ensure_service(app_name, service_ports) -> cluster_ip`
    - `update_endpoints(app_name, {port: [(ip, target_port), ...]})`
    - `remove_service(app_name)`
  - Proxy image: `AE_SERVICE_PROXY_IMAGE` (default `haproxy:2.9-alpine`).
- Controller integration
  - Prefer Service IP as ingress upstream: `src/ae/controller/reconciler.py`.
  - Ingress writer uses a single upstream when Service exists: `src/ae/ingress/service.py`.
- State store
  - Add tables and helpers in `src/ae/controller/state.py`:
    - `services(app_name TEXT PK, cluster_ip TEXT, ports TEXT, created_at TEXT)`
    - `service_endpoints(app_name TEXT, port INT, ip TEXT, target_port INT, ready INT, PRIMARY KEY(app_name, port, ip))`

Spec Surface (No breaking changes)
- Extend `ServiceSpec` with controller‑owned fields (ignored on input):
  - `clusterIP` (string), `clusterIPs` (list). File: `src/ae/controller/spec.py`.

Config & Env Vars
- `AE_NETWORK_NAME=ae-net`
- `AE_NETWORK_SUBNET=10.241.0.0/16`
- `AE_SERVICE_IP_POOL=10.241.0.0/16` (same or sibling of network subnet)
- `AE_SERVICE_PROXY_IMAGE=haproxy:2.9-alpine`

Testing
- Integration: deploy multi‑replica echo with Service; curl Service IP from another container on `ae-net`.
- Rollouts: verify backends switch post‑readiness; canary/auto progression continues to function.
- Failure: remove one replica; proxy drains it from backends.

Risks/Notes
- Static IPs require IPAM tracking and collision handling.
- Keep proxy config minimal; prefer hot reloads; avoid per‑request health checks (we already gate on readiness).

---

## Phase 2 — Provider Abstraction

Deliverables
- Introduce `NetworkProvider` interface and a `ServiceController` that depends on it.
- Move Docker‑specific logic into `DockerBridgeProvider`.
- Keep controller/ingress behavior stable; only the provider changes.

New Modules
- `src/ae/network/provider.py` (interface):
  - `ensure_network() -> None`
  - `allocate_service_ip(app_name) -> str`
  - `release_service_ip(app_name) -> None`
  - `ensure_service_proxy(app_name, ports) -> str` (returns cluster_ip)
  - `update_service_proxy(app_name, backends_by_port) -> None`
  - `remove_service_proxy(app_name) -> None`

Refactors
- `DockerBridgeProvider` implements the above using Docker networking and the HAProxy container.
- `ServiceController` orchestrates allocation, endpoint watching, and proxy updates.

---

## Phase 3 — Multi‑Node Ready Provider

Deliverables
- Add a `KubeProxyLikeProvider` that implements ClusterIP via:
  - iptables/IPVS programming per node, or
  - a per‑node Envoy/xtables proxy with a real Service CIDR routed between nodes.
- Introduce a pod IP model (either routed bridge per node or a simple CNI/overlay).

Dependencies
- A routable overlay (e.g., wireguard/flannel) or L3 routing for container subnets.
- Service CIDR reserved and routed across nodes.

Ingress Integration
- No change: Ingress keeps targeting Service IPs.

Security & Ops
- Namespace network policy is out of scope initially; document constraints.
- Validate iptables/IPVS changes via a dry‑run mode before applying.

Testing
- Multi‑node lab with two hosts; backends on each node.
- Failure domains: stop one node; confirm service continuity when replicas exist on remaining nodes.

---

## Exporter Compatibility

- `export-k8s` emission stays unchanged and portable: we continue to emit a standard ClusterIP Service without `clusterIP` so upstream allocates it.
- The runtime’s ClusterIP emulation is local‑only and transparent to exporter users.

---

## Task Breakdown (Issues Checklist)

1) Data model and config
- [ ] Add `services` and `service_endpoints` tables and helpers in `src/ae/controller/state.py`.
- [ ] Add envs and defaults (CLI/docs): `AE_NETWORK_NAME`, `AE_NETWORK_SUBNET`, `AE_SERVICE_IP_POOL`, `AE_SERVICE_PROXY_IMAGE`.

2) Runtime network and IPAM
- [ ] Ensure/create Docker bridge with IPAM in `src/ae/runtime/docker_runtime.py` and always connect app containers.
- [ ] Add a simple Service IP allocator (persist to DB) with reclaim on delete.

3) Service proxy
- [ ] `src/ae/network/service_proxy.py`: start/stop proxy container, render HAProxy config, hot‑reload, static IP assignment.
- [ ] Health‑gate backends from controller readiness.

4) Controller/Ingress integration
- [ ] `src/ae/controller/reconciler.py`: prefer Service IP for upstreams when Service exists.
- [ ] `src/ae/ingress/service.py`: accept Service IP:port single upstream.

5) Spec and validation
- [ ] Extend `ServiceSpec` with controller‑owned `clusterIP/clusterIPs` (ignored on input) in `src/ae/controller/spec.py`.
- [ ] Document Service semantics in `docs/reference/architecture.md`.

6) Tests and demos
- [ ] Add integration tests for Service IP routing and rollouts.
- [ ] Provide a demo manifest under `specs/examples/` and a short walkthrough in docs.

7) Abstraction (Phase 2)
- [ ] Define `NetworkProvider` interface and plug a `DockerBridgeProvider` behind `ServiceController`.

8) Multi‑node (Phase 3)
- [ ] Spike a `KubeProxyLikeProvider` using iptables/IPVS or per‑node Envoy and a routed Service CIDR.
- [ ] Write a two‑node lab doc and success checklist.

---

## Rollback Plan

- Feature flag the Service controller and proxy (`AE_ENABLE_SERVICE_PROXY=0` by default until stable).
- On disable, controller reverts ingress to direct replica endpoints and deletes proxy containers; state tables remain for forensics.
