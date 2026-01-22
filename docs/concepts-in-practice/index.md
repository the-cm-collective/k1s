<div class="hero">
  <div class="hero-brand">
    <div class="hero-logo-row">
      <img src="static/k1s-logo-horizontal.svg" alt="k1s logo" class="hero-logo" />
      <span class="hero-pill">Concepts in Practice</span>
    </div>
    <h1>Concepts in Practice</h1>
    <p class="hero-tagline">Hands-on, chapterized walkthroughs for k1s orchestration concepts, mapped to Kubernetes equivalents.</p>
    <div class="hero-links">
      <a class="hero-link" href="start-here.html">Start Here</a>
      <a class="hero-link" href="concepts.html">Concepts Overview</a>
      <a class="hero-link" href="multinode-lab.html">Multi-Node Lab</a>
    </div>
  </div>
  <div class="hero-actions">
    <div class="hero-card">
      <h2>Core Control Loop</h2>
      <p>Spec, apply, and placement foundations.</p>
      <div class="hero-links hero-links--dense">
        <a class="hero-link hero-link--stack" href="concepts-in-practice-01-desired-state-reconciliation.html">
          <span class="hero-link-title">Chapter 01: Desired State &amp; Reconciliation</span>
          <span class="hero-link-sub">Explain how k1s continuously converges actual runtime state to the declared spec, and map that pattern to Kubernetes controllers.</span>
        </a>
        <a class="hero-link hero-link--stack" href="concepts-in-practice-02-declarative-apply.html">
          <span class="hero-link-title">Chapter 02: Declarative Specs &amp; Apply</span>
          <span class="hero-link-sub">Show how a declarative spec becomes the single source of truth, and how apply merges desired state into the controller's registry.</span>
        </a>
        <a class="hero-link hero-link--stack" href="concepts-in-practice-03-scheduling-placement.html">
          <span class="hero-link-title">Chapter 03: Scheduling &amp; Placement</span>
          <span class="hero-link-sub">Explain how k1s decides replica placement and how that maps to Kubernetes scheduler behavior.</span>
        </a>
      </div>
    </div>
    <div class="hero-card">
      <h2>Runtime &amp; Exposure</h2>
      <p>Containers, ingress, and service routing.</p>
      <div class="hero-links hero-links--dense">
        <a class="hero-link hero-link--stack" href="concepts-in-practice-04-runtime-adapters.html">
          <span class="hero-link-title">Chapter 04: Runtime Adapters</span>
          <span class="hero-link-sub">Trace how k1s translates a manifest into runtime operations and how adapters make that portable across container engines.</span>
        </a>
        <a class="hero-link hero-link--stack" href="concepts-in-practice-05-ingress-service-exposure.html">
          <span class="hero-link-title">Chapter 05: Ingress &amp; Services</span>
          <span class="hero-link-sub">Walk through how k1s exposes services: L4 Service VIPs and L7 ingress via Caddy, then map to k8s Services and Ingress/Gateway.</span>
        </a>
      </div>
    </div>
    <div class="hero-card">
      <h2>Reliability &amp; Rollouts</h2>
      <p>Observability, probes, and update safety.</p>
      <div class="hero-links hero-links--dense">
        <a class="hero-link hero-link--stack" href="concepts-in-practice-06-observability.html">
          <span class="hero-link-title">Chapter 06: Observability</span>
          <span class="hero-link-sub">Teach how to inspect k1s state using metrics snapshots and event streams, and map that to k8s observability patterns.</span>
        </a>
        <a class="hero-link hero-link--stack" href="concepts-in-practice-07-health-probes.html">
          <span class="hero-link-title">Chapter 07: Health Probes</span>
          <span class="hero-link-sub">Explain how readiness/liveness/startup probes gate traffic and restarts, and show how k1s evaluates probe state.</span>
        </a>
        <a class="hero-link hero-link--stack" href="concepts-in-practice-08-rollouts-updates.html">
          <span class="hero-link-title">Chapter 08: Rollouts &amp; Rollbacks</span>
          <span class="hero-link-sub">Show how k1s performs controlled updates, tracks revisions, and supports rollbacks, then map to k8s Deployment rollouts.</span>
        </a>
      </div>
    </div>
    <div class="hero-card">
      <h2>Config &amp; Policy</h2>
      <p>Secrets, access, and enforcement boundaries.</p>
      <div class="hero-links hero-links--dense">
        <a class="hero-link hero-link--stack" href="concepts-in-practice-09-configuration-secrets.html">
          <span class="hero-link-title">Chapter 09: Configs &amp; Secrets</span>
          <span class="hero-link-sub">Show how k1s loads configs and sealed secrets, projects them into env/files, and maps that to k8s ConfigMaps/Secrets.</span>
        </a>
        <a class="hero-link hero-link--stack" href="concepts-in-practice-10-access-policy.html">
          <span class="hero-link-title">Chapter 10: Access &amp; Policy</span>
          <span class="hero-link-sub">Explain how k1s enforces API access roles, registry credentials, and node join tokens, then map that to k8s RBAC and admission controls.</span>
        </a>
      </div>
    </div>
  </div>
</div>
