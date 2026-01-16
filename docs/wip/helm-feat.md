# Helm Shim Next Steps (Plan)

## Goals
- Make Helm install/upgrade/uninstall flows reliable enough for common charts.
- Reduce false-positives by aligning shim behavior with Helm’s expectations for release storage and selectors.
- Document explicit scope boundaries so users know when to use real Kubernetes or `helm template`.

## Scope Target
- In-scope: charts that use Deployment/Service/Ingress/HPA/RBAC + simple CRDs (no external controllers), and that rely on Helm release storage (`helm ls`, `history`, `rollback`).
- Out-of-scope (for now): operators/controllers, admission webhooks, CSI/CNI, PodSecurity, advanced ingress features, persistent StatefulSet semantics, and Jobs/CronJobs with real lifecycle guarantees.

## Completed (moved to `docs/adr/0013-helm-shim-correctness-and-ci-gate.md`)
- [x] 1) Replace “template-only” demo with real Helm install/upgrade/uninstall.
- [x] 2) Implement label/field selector filtering for list/watch (Helm storage correctness).
- [x] 3) Fix Service → App target resolution to honor full selector.
- [x] 4) Clarify and harden workload semantics (Jobs/CronJobs/StatefulSets/DaemonSets).
- [x] 5) Add a Helm compatibility gate in CI (smoke + regression).

## Plan (remaining)

### 6) Extend workload support beyond current limitations (Jobs/CronJobs/StatefulSets/DaemonSets/PVCs)
**Why:** The shim currently emulates several workload kinds as Deployment-like apps. Additive features can bring these closer to real Kubernetes semantics so more charts run without a real cluster.

**Work items**
- Jobs (batch/v1)
  - Track per-Job pods/replicas and compute status fields (`active`, `succeeded`, `failed`, `conditions`).
  - Enforce `parallelism`, `completions`, and `backoffLimit` based on runtime exit codes.
  - Honor `ttlSecondsAfterFinished` by deleting job-derived resources after completion.
  - Add events for `Complete`/`Failed` transitions and expose in `ae events`.
- CronJobs (batch/v1)
  - Parse `spec.schedule` using a real cron parser; respect `startingDeadlineSeconds`.
  - Create distinct Job objects per run, name-suffixed by scheduled timestamp.
  - Track `lastScheduleTime`, `lastSuccessfulTime`, and respect history limits.
  - Garbage-collect old Jobs per `successfulJobsHistoryLimit`/`failedJobsHistoryLimit`.
- StatefulSets (apps/v1)
  - Implement stable ordinal identity (`<name>-0`, `<name>-1`) and ordered create/update/delete.
  - Support `podManagementPolicy` (`OrderedReady` vs `Parallel`) and rolling updates.
  - Persist per-ordinal identity in runtime so restarts map to the same replica.
  - Honor `serviceName` for stable DNS (via headless service mapping).
- PersistentVolumeClaims / Volumes
  - Add a minimal PV/PVC controller with a local storage class (e.g., hostPath-backed).
  - Bind PVCs to PVs, create per-claim directories, and inject mounts into runtime containers.
  - Support access modes (`ReadWriteOnce`) and basic capacity accounting.
  - Map `volumeClaimTemplates` in StatefulSets to per-ordinal PVCs.
- DaemonSets (apps/v1)
  - Schedule one replica per node and reconcile on node add/remove.
  - Track status fields (`desiredNumberScheduled`, `currentNumberScheduled`, `numberReady`).
  - Respect `updateStrategy` (rolling update with maxUnavailable).

**Acceptance checks**
- Jobs complete with accurate status; `kubectl/helm --wait` unblocks for Job-based charts.
- CronJobs create Jobs on schedule and prune history as configured.
- StatefulSets preserve identity across restarts and roll out in order.
- PVCs bind and mount correctly; data persists across pod restarts.
- DaemonSets converge to one pod per node and update safely on config changes.

## Milestones
- **M1 (shim correctness):** Done (Items 1–3, 2026-01-16).
- **M2 (documented boundaries):** Done (Item 4, 2026-01-16).
- **M3 (regression safety):** Done (Item 5, 2026-01-16).
- **M4 (workload parity):** Item 6 complete.

## Risks & Notes
- Selector support needs to be consistent across LIST and WATCH to avoid Helm cache issues.
- Release storage defaults vary by Helm version (secrets vs configmaps); support both.
- Keep the demo script non-destructive and ensure it cleans up its shim process reliably.
