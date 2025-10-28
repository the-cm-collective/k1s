┌──(.venv-demo)─(m4xx3d0ut㉿h4ckt0p)-[~/git/k1s]
└─$ make bench-mem-e2e-k1s LABEL_SUITE=report-20251028 APP=specs/examples/echo.yaml REPLICAS=1,5,10 DURATION=30 ROLL_REPLICAS=5
[matrix] idle snapshot
[mem-snapshot] mode=k1s label=report-20251028-idle duration=30s -> snapshots/report-20251028-idle/20251027-185654
[mem-snapshot] done -> snapshots/report-20251028-idle/20251027-185654
snapshots/report-20251028-idle/20251027-185654
[matrix] apply manifest: specs/examples/echo.yaml
2025-10-27 18:57:27 WARNING ae.ingress.service: ingress reload skipped: Caddy reload dependency not found: caddy
Applied echo: +0/~0/-0, ready=1, live=1, rev=6713(ready)
[matrix] scale echo to 1
2025-10-27 18:57:30 WARNING ae.ingress.service: ingress reload skipped: Caddy reload dependency not found: caddy
scaled echo to replicas=1: rev=6713(ready) ops=+0/~0/-0 ready=1/1
[matrix] snapshot label=report-20251028-pods-1
[mem-snapshot] mode=k1s label=report-20251028-pods-1 duration=30s -> snapshots/report-20251028-pods-1/20251027-185731
[mem-snapshot] done -> snapshots/report-20251028-pods-1/20251027-185731
snapshots/report-20251028-pods-1/20251027-185731
[matrix] scale echo to 5
scaled echo to replicas=5: rev=6714(progressing) ops=+5/~0/-0 ready=0/0
timeout waiting for echo ready=5
[matrix] snapshot label=report-20251028-pods-5
[mem-snapshot] mode=k1s label=report-20251028-pods-5 duration=30s -> snapshots/report-20251028-pods-5/20251027-190017
[mem-snapshot] done -> snapshots/report-20251028-pods-5/20251027-190017
snapshots/report-20251028-pods-5/20251027-190017
[matrix] scale echo to 10
scaled echo to replicas=10: rev=6716(progressing) ops=+10/~0/-0 ready=0/0
timeout waiting for echo ready=10
[matrix] snapshot label=report-20251028-pods-10
[mem-snapshot] mode=k1s label=report-20251028-pods-10 duration=30s -> snapshots/report-20251028-pods-10/20251027-190304
[mem-snapshot] done -> snapshots/report-20251028-pods-10/20251027-190304
snapshots/report-20251028-pods-10/20251027-190304
[matrix] done
[rollout] scale echo to 5 and wait ready
Applied echo: +1/~0/-0, ready=0, live=0, rev=6717(progressing)
scaled echo to replicas=5: rev=6718(progressing) ops=+5/~0/-0 ready=0/0
timeout waiting for echo ready=5
[rollout] apply new image: demo-blue:latest
2025-10-27 19:08:12 WARNING ae.ingress.service: ingress reload skipped: Caddy reload dependency not found: caddy
Applied echo: +0/~0/-0, ready=1, live=1, rev=6719(ready)
[rollout] snapshot DURING rollout
[mem-snapshot] mode=k1s label=baseline-roll-rollout-5-during duration=30s -> snapshots/baseline-roll-rollout-5-during/20251027-191205
[mem-snapshot] done -> snapshots/baseline-roll-rollout-5-during/20251027-191205
snapshots/baseline-roll-rollout-5-during/20251027-191205
[rollout] wait ready post-rollout
timeout waiting for echo ready=5
[rollout] snapshot POST rollout
[mem-snapshot] mode=k1s label=baseline-roll-rollout-5-post duration=30s -> snapshots/baseline-roll-rollout-5-post/20251027-191734
[mem-snapshot] done -> snapshots/baseline-roll-rollout-5-post/20251027-191734
snapshots/baseline-roll-rollout-5-post/20251027-191734
[rollout] done
wrote combined/combined.json and combined/combined.csv
wrote charts to charts





┌──(.venv-demo)─(m4xx3d0ut㉿h4ckt0p)-[~/git/k1s]
└─$ python -m ae.cli status echo --wide --events
echo: desired=1, ready=1, live=1, rev=6719(ready), image=demo-blue:latest, ops=+0/~0/-0, ingress=echo.home.arpa/
  - echo-rev6719-0: ready=True live=True status=running | readiness=readiness http 200; liveness=liveness http 200
    event 2025-10-28 02:17:46 rev=6719 ApplyCompleted: Revision 6719 status ready
    event 2025-10-28 02:17:46 rev=6719 IngressConfigured: Ingress upstreams set to 127.0.0.1:33255
    event 2025-10-28 02:17:42 rev=6719 ApplyStarted: Reconciling revision 6719
    event 2025-10-28 02:12:05 rev=6719 ApplyCompleted: Revision 6719 status ready
    event 2025-10-28 02:12:05 rev=6719 RolloutOldRemoved: Removed 23 old revision container(s)
    event 2025-10-28 02:12:05 rev=6719 ApplyCompleted: Revision 6719 status ready
    event 2025-10-28 02:12:05 rev=6719 RolloutOldRemoved: Removed 12 old revision container(s)
    event 2025-10-28 02:10:05 rev=6719 IngressConfigured: Ingress upstreams set to 127.0.0.1:33255
    event 2025-10-28 02:10:02 rev=6719 ApplyStarted: Reconciling revision 6719
    event 2025-10-28 02:08:12 rev=6719 IngressConfigured: Ingress upstreams set to 127.0.0.1:33255
