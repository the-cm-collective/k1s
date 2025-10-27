Title: k8s-alignment phase 1: SecuritySpec, probe windowing, TCP probes, planner JSON, demos

Summary
- Add SecuritySpec (runAsUser/runAsGroup/readOnlyRootFilesystem/dropCapabilities) and map to Docker/Podman.
- Implement probe windowing (success/failure thresholds, periodSeconds cache).
- Add TCP socket probe support.
- Planner warnings for alignment + --json output for CI gating.
- Demos: echo-sec (security) and echo-tcp (TCP probe); init flags to apply.

Changes
- spec: SecuritySpec, terminationGracePeriodSeconds, TCP probes.
- runtime: Docker/Podman security flags; stop timeout label.
- health: windowing logic; TCP probe.
- cli: plan warnings + --json output.
- scripts: init_demo flags for security/TCP demos.
- docs: K8S_PARITY.md + examples, CI workflow, planner gating script.

Impact/Rollout
- Backward compatible; defaults preserve prior behavior.
- Status classification improvements already merged; no migration required.

Verification
- pytest passes.
- Planner warnings appear for missing readiness/security, multi-replica service usage.
- Demo apply: echo-sec and echo-tcp become ready; dashboard logs stream.

Screenshots/Logs
<!-- attach dashboard/status outputs as needed -->

Checklist
- [ ] Docs reviewed (K8S_PARITY.md)
- [ ] Demos apply successfully
- [ ] CI green

