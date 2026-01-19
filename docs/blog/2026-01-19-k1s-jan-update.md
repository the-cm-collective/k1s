---
title: "k1s January Update: Multi-node, Service VIPs, and an Expanded API Shim"
date: 2026-01-19
authors: [k1s Team]
tags: [k1s, updates, multinode, apishim, services, observability, security]
summary: "Since the November update, k1s added multi-node support with agent-backed runtimes, Service VIPs, broader Kubernetes API shim parity (exec/port-forward/HPA), and expanded security and observability coverage."
cover_image: "../docs.home.arpa_8443_dashboard.png"
---

# k1s January Update: The Project, Multi-node, Service VIPs, and an Expanded API Shim

This is a short technical tour of changes since the November update. We have made some significant progress on multi-node support, Service VIPs, API shim parity, security, observability, and the tooling that holds it together, but first I'd like to talk about the project in general.

It was your classic case of "need something to do something" vs "curiosity about how the thing does the thing". You know, typical stuff. I needed a small application engine to run on a VPS with minimal resources and I was already trying to broaden my understanding of Kubernetes and app engines in general. I already knew that k3s was a little too heavy for my VPS. I also had some free time on my hands, so I dove in. I started with a small set of success criteria that made me feel good about things:

- lightweight, able to run where k3s was too heavy
- k8s-like, with good portability to k3s/k8s
- approachable for newcomers to AE, easy to clone and spin up a lab stack
- integrated dashboard for monitoring and an interactive "playground" to help visualize how things work

I feel like it's starting to meet the mark and I'm certainly having fun with it!

A single controller process gets you a working system, SQLite is the default state store (Postgres is optional), and the runtime surface stays small enough to be useful without dragging in a full Kubernetes control plane. The API shim keeps common kubectl/helm workflows usable, and portability checks (`ae k8s-check`) plus export helpers (`ae export-k8s`) keep the YAML close enough to move into k3s/k8s when you need to.

You can clone, install, run the examples in `specs/examples/`, then follow `docs/guides/e2e.md` or `docs/guides/multinode-lab.md` to build a lab stack or just run `make demo`. The integrated dashboard gives live status/events/logs plus a system snapshot, and the Labs playground runs read-only by default with optional tokens when you want to allow safe actions, plus graphs to visualize the system state.

There is enough substance here to be useful, but it is still a work in progress. I run the system in a private cloud/lab setting for development and testing, but I'm clearly not at a point where I would recommend it for production use. As far as the future of the project is concerned, there is much to be decided, but I do know a few things for sure. I'm going to continue to develop the system and will gladly answer questions and take feedback. I'm also open to collaborating with others who are interested in cloud/edge or working with contributors who also get a kick out of this kind of thing. Long-term, I'm considering a path to CNCF conformance certification, but there are some capabilities I'd like to experiment with that may not fit the mold, so maybe this is going to be something... different. Too early to say, but for now I'll just enjoy exercising my craft, simply for the love of it!

## TL;DR Highlights

- Multi-node plumbing: new `ae-node` agent, RemoteRuntime adapter, and scheduler placement across Ready nodes.
- Service VIPs: Service controller plus per-Service HAProxy sidecars with ClusterIP allocation for multi-replica routing.
- API shim parity: pods/logs/exec/port-forward, HPA, nodes/endpoints, RBAC, ServiceAccount tokens, JSONPatch/Apply, and list/watch semantics.
- Security: mTLS for agents, join-token tracking, and cert rotation tooling.
- Observability: node inventory and service endpoint readiness metrics, plus dashboard views for placement and node health.
- Tooling/docs: multi-node lab scripts, QEMU smoke coverage, and benchmark + docs site updates.

## 1) Multi-node: Agent, Remote Runtime, and Scheduler

We added `ae-node` plus a RemoteRuntime adapter so the controller can delegate container lifecycle and log/exec calls to workers. The controller keeps node inventory and heartbeats, auto-registers the local node for single-node runs, and schedules replicas across Ready nodes with nodeSelector/tolerations and storage pinning. Think of it as the same reconcile loop, just spread across machines.

The multi-node lab guide walks through a two-host setup (controller + worker) with WireGuard-backed overlay networking and the agent API. The controller exposes an agent API (defaults to `:9110`) that can be token-gated with `AE_AGENT_API_TOKEN`, moved via `AE_AGENT_API_PORT`, and optionally secured with mTLS (`AE_AGENT_API_TLS_CERT/KEY`, `AE_AGENT_API_CLIENT_CA`, `AE_AGENT_API_REQUIRE_CLIENT_CERT=1`).

Example (see `docs/guides/multinode-lab.md` for the full env and TLS options):

```bash
# Controller with agent API enabled (add TLS vars if needed)
AE_AGENT_API_TOKEN=REDACTED