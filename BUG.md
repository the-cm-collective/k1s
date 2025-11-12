┌──(.venv-demo)─(m4xx3d0ut㉿h4ckt0p)-[~/git/k1s]
└─$ sudo make bench-engines-clear CONFIRM=1       
[engines-clear] starting (confirm=1)
[engines-clear] docker: clear (0 containers)
[engines-clear] podman: clear (0 containers)
[engines-clear] engines clear: OK
                                                  
┌──(.venv-demo)─(m4xx3d0ut㉿h4ckt0p)-[~/git/k1s]
└─$ make bench-mem-e2e-baselines-sudo WAIT_READY_TRIES=300 REPLICAS=1 DURATION=30
[14:09:03] sudo enabled for privileged steps
[14:09:03] starting baseline at 2025-11-11T14:09:03
[14:09:03] suite: k1s rootless
[14:09:03] clearing container engines (rootless/rootful)
[14:09:03] stopping any running controllers (user/root)
[14:09:04] rootless podman: stopping/removing containers
[14:09:16] make bench-engines-clear (sudo)
[14:09:16] starting caddy (docker)
[14:09:16] building demo-blue:latest (rootless)
[14:09:17] building demo-green:latest (rootless)
make[1]: Entering directory '/home/m4xx3d0ut/git/k1s'
[matrix] controller not detected; attempting auto-start...
[matrix] controller started (logs: /tmp/k1s_ctrl_bench.m4xx3d0ut.3144227.log)
[matrix] idle snapshot
[mem-snapshot] mode=k1s label=r20251110+podman+rootless+cg2-idle duration=30s -> snapshots/r20251110+podman+rootless+cg2-idle/20251111-140925
[mem-snapshot] start: outdir=snapshots/r20251110+podman+rootless+cg2-idle/20251111-140925
[mem-snapshot] write meta and preflight
[mem-snapshot] meta.json written
[mem-snapshot] ps snapshots captured
[mem-snapshot] process smaps/status captured
[mem-snapshot] podman containers collected (if selected)
[mem-snapshot] docker containers collected (if selected)
[mem-snapshot] collection complete; aggregating
[mem-snapshot] done -> snapshots/r20251110+podman+rootless+cg2-idle/20251111-140925
[mem-snapshot] aggregation ok
snapshots/r20251110+podman+rootless+cg2-idle/20251111-140925
[matrix] apply manifest: specs/examples/blue.yaml
2025-11-11 14:10:03 WARNING ae.ingress.service: ingress reload skipped: Caddy reload dependency not found: caddy
Applied blue: +0/~0/-0, ready=1, live=1, rev=10(ready)
[matrix] scale blue to 1
2025-11-11 14:10:08 WARNING ae.ingress.service: ingress reload skipped: Caddy reload dependency not found: caddy
scaled blue to replicas=1: rev=10(ready) ops=+0/~0/-0 ready=1/1
[matrix] snapshot label=r20251110+podman+rootless+cg2-pods-1
[mem-snapshot] mode=k1s label=r20251110+podman+rootless+cg2-pods-1 duration=30s -> snapshots/r20251110+podman+rootless+cg2-pods-1/20251111-141009
[mem-snapshot] start: outdir=snapshots/r20251110+podman+rootless+cg2-pods-1/20251111-141009
[mem-snapshot] write meta and preflight
[mem-snapshot] meta.json written
[mem-snapshot] ps snapshots captured
[mem-snapshot] process smaps/status captured
[mem-snapshot] podman containers collected (if selected)
[mem-snapshot] docker containers collected (if selected)
[mem-snapshot] collection complete; aggregating
[mem-snapshot] done -> snapshots/r20251110+podman+rootless+cg2-pods-1/20251111-141009
[mem-snapshot] aggregation ok
snapshots/r20251110+podman+rootless+cg2-pods-1/20251111-141009
[matrix] done
[rollout] scale blue to 5 and wait ready
2025-11-11 14:10:52 WARNING ae.ingress.service: ingress reload skipped: Caddy reload dependency not found: caddy
Applied blue: +0/~0/-0, ready=1, live=1, rev=10(ready)
2025-11-11 14:10:59 WARNING ae.ingress.service: ingress reload skipped: Caddy reload dependency not found: caddy
scaled blue to replicas=5: rev=11(progressing) ops=+5/~0/-0 ready=2/0
timeout waiting for blue ready=5
[rollout] apply new image: demo-blue:latest
2025-11-11 14:23:57 WARNING ae.ingress.service: ingress reload skipped: Caddy reload dependency not found: caddy
Applied blue: +0/~0/-0, ready=1, live=1, rev=12(ready)
[rollout] snapshot DURING rollout
[mem-snapshot] mode=k1s label=r20251110+podman+rootless+cg2-rollout-5-during duration=30s -> snapshots/r20251110+podman+rootless+cg2-rollout-5-during/20251111-142358
[mem-snapshot] start: outdir=snapshots/r20251110+podman+rootless+cg2-rollout-5-during/20251111-142358
[mem-snapshot] write meta and preflight
[mem-snapshot] meta.json written
[mem-snapshot] ps snapshots captured
[mem-snapshot] process smaps/status captured
[mem-snapshot] podman containers collected (if selected)
[mem-snapshot] docker containers collected (if selected)
[mem-snapshot] collection complete; aggregating
[mem-snapshot] done -> snapshots/r20251110+podman+rootless+cg2-rollout-5-during/20251111-142358
[mem-snapshot] aggregation ok
snapshots/r20251110+podman+rootless+cg2-rollout-5-during/20251111-142358
[rollout] wait ready post-rollout

timeout waiting for blue ready=5
[rollout] snapshot POST rollout
[mem-snapshot] mode=k1s label=r20251110+podman+rootless+cg2-rollout-5-post duration=30s -> snapshots/r20251110+podman+rootless+cg2-rollout-5-post/20251111-143718
[mem-snapshot] start: outdir=snapshots/r20251110+podman+rootless+cg2-rollout-5-post/20251111-143718
[mem-snapshot] write meta and preflight
[mem-snapshot] meta.json written
[mem-snapshot] ps snapshots captured
[mem-snapshot] process smaps/status captured
[mem-snapshot] podman containers collected (if selected)
[mem-snapshot] docker containers collected (if selected)
[mem-snapshot] collection complete; aggregating
[mem-snapshot] done -> snapshots/r20251110+podman+rootless+cg2-rollout-5-post/20251111-143718
[mem-snapshot] aggregation ok
snapshots/r20251110+podman+rootless+cg2-rollout-5-post/20251111-143718
[rollout] done
wrote combined/combined.json and combined/combined.csv
/home/m4xx3d0ut/git/k1s/scripts/bench/plot_overhead.py:136: UserWarning: Attempting to set identical low and high ylims makes transformation singular; automatically expanding.
  ax.set_ylim(0, max(ylim[1], max(data) * 1.1))
wrote charts to charts
make[1]: Leaving directory '/home/m4xx3d0ut/git/k1s'
[14:37:58] normalizing artifact permissions
[sudo] password for m4xx3d0ut:
[14:38:08] suite: k1s rootful (sudo)
[14:38:08] clearing container engines (rootless/rootful)
[14:38:08] stopping any running controllers (user/root)
[14:38:09] rootless podman: stopping/removing containers
[14:38:20] make bench-engines-clear (sudo)
[14:38:20] starting caddy (docker)
[14:38:21] building demo-blue:latest (rootful)
[14:38:27] building demo-green:latest (rootful)
make[1]: Entering directory '/home/m4xx3d0ut/git/k1s'
[matrix] controller not detected; attempting auto-start...
[matrix] controller started (logs: /tmp/k1s_ctrl_bench.root.3532003.log)
[matrix] idle snapshot
[mem-snapshot] mode=k1s label=r20251110+podman+priv+cg2-idle duration=30s -> snapshots/r20251110+podman+priv+cg2-idle/20251111-143836
[mem-snapshot] start: outdir=snapshots/r20251110+podman+priv+cg2-idle/20251111-143836
[mem-snapshot] write meta and preflight
[mem-snapshot] meta.json written
[mem-snapshot] ps snapshots captured
[mem-snapshot] process smaps/status captured
[mem-snapshot] podman containers collected (if selected)
[mem-snapshot] docker containers collected (if selected)
[mem-snapshot] collection complete; aggregating
[mem-snapshot] done -> snapshots/r20251110+podman+priv+cg2-idle/20251111-143836
[mem-snapshot] aggregation ok
snapshots/r20251110+podman+priv+cg2-idle/20251111-143836
[matrix] apply manifest: specs/examples/blue.yaml
2025-11-11 14:39:15 WARNING ae.ingress.service: ingress reload skipped: Caddy reload dependency not found: caddy
Applied blue: +0/~0/-0, ready=1, live=1, rev=12(ready)
[matrix] scale blue to 1
2025-11-11 14:39:22 WARNING ae.ingress.service: ingress reload skipped: Caddy reload dependency not found: caddy
scaled blue to replicas=1: rev=12(ready) ops=+0/~0/-0 ready=1/1
[matrix] snapshot label=r20251110+podman+priv+cg2-pods-1
[mem-snapshot] mode=k1s label=r20251110+podman+priv+cg2-pods-1 duration=30s -> snapshots/r20251110+podman+priv+cg2-pods-1/20251111-143924
[mem-snapshot] start: outdir=snapshots/r20251110+podman+priv+cg2-pods-1/20251111-143924
[mem-snapshot] write meta and preflight
[mem-snapshot] meta.json written
[mem-snapshot] ps snapshots captured
[mem-snapshot] process smaps/status captured
[mem-snapshot] podman containers collected (if selected)
[mem-snapshot] docker containers collected (if selected)
[mem-snapshot] collection complete; aggregating
[mem-snapshot] done -> snapshots/r20251110+podman+priv+cg2-pods-1/20251111-143924
[mem-snapshot] aggregation ok
snapshots/r20251110+podman+priv+cg2-pods-1/20251111-143924
[matrix] done
[rollout] scale blue to 5 and wait ready
2025-11-11 14:40:08 WARNING ae.ingress.service: ingress reload skipped: Caddy reload dependency not found: caddy
Applied blue: +0/~0/-0, ready=1, live=1, rev=12(ready)
2025-11-11 14:40:21 WARNING ae.ingress.service: ingress reload skipped: Caddy reload dependency not found: caddy
scaled blue to replicas=5: rev=13(progressing) ops=+5/~0/-0 ready=4/0
timeout waiting for blue ready=5
[rollout] apply new image: demo-blue:latest
2025-11-11 14:53:15 WARNING ae.ingress.service: ingress reload skipped: Caddy reload dependency not found: caddy
Applied blue: +0/~0/-0, ready=1, live=1, rev=14(ready)
[rollout] snapshot DURING rollout
[mem-snapshot] mode=k1s label=r20251110+podman+priv+cg2-rollout-5-during duration=30s -> snapshots/r20251110+podman+priv+cg2-rollout-5-during/20251111-145316
[mem-snapshot] start: outdir=snapshots/r20251110+podman+priv+cg2-rollout-5-during/20251111-145316
[mem-snapshot] write meta and preflight
[mem-snapshot] meta.json written
[mem-snapshot] ps snapshots captured
[mem-snapshot] process smaps/status captured
[mem-snapshot] podman containers collected (if selected)
[mem-snapshot] docker containers collected (if selected)
[mem-snapshot] collection complete; aggregating
[mem-snapshot] done -> snapshots/r20251110+podman+priv+cg2-rollout-5-during/20251111-145316
[mem-snapshot] aggregation ok
snapshots/r20251110+podman+priv+cg2-rollout-5-during/20251111-145316
[rollout] wait ready post-rollout

timeout waiting for blue ready=5
[rollout] snapshot POST rollout
[mem-snapshot] mode=k1s label=r20251110+podman+priv+cg2-rollout-5-post duration=30s -> snapshots/r20251110+podman+priv+cg2-rollout-5-post/20251111-150529
[mem-snapshot] start: outdir=snapshots/r20251110+podman+priv+cg2-rollout-5-post/20251111-150529
[mem-snapshot] write meta and preflight
[mem-snapshot] meta.json written
[mem-snapshot] ps snapshots captured
[mem-snapshot] process smaps/status captured
[mem-snapshot] podman containers collected (if selected)
[mem-snapshot] docker containers collected (if selected)
[mem-snapshot] collection complete; aggregating
[mem-snapshot] done -> snapshots/r20251110+podman+priv+cg2-rollout-5-post/20251111-150529
[mem-snapshot] aggregation ok
snapshots/r20251110+podman+priv+cg2-rollout-5-post/20251111-150529
[rollout] done
wrote combined/combined.json and combined/combined.csv
/home/m4xx3d0ut/git/k1s/scripts/bench/plot_overhead.py:136: UserWarning: Attempting to set identical low and high ylims makes transformation singular; automatically expanding.
  ax.set_ylim(0, max(ylim[1], max(data) * 1.1))
wrote charts to charts
make[1]: Leaving directory '/home/m4xx3d0ut/git/k1s'
[15:06:05] normalizing artifact permissions
[sudo] password for m4xx3d0ut:
[15:07:20] suite: k1nd
[15:07:20] clearing container engines (rootless/rootful)
[15:07:20] stopping any running controllers (user/root)
[15:07:20] rootful podman: stopping/removing containers (sudo)
[15:07:33] make bench-engines-clear (sudo)
make[1]: Entering directory '/home/m4xx3d0ut/git/k1s'
make[2]: Entering directory '/home/m4xx3d0ut/git/k1s'
docker compose -f ops/dev/labs-aio.yaml up -d
[+] Running 1/2
 ⠹ Container dev-controller-1  Starting      0.2s
 ✔ Container dev-caddy-1       Created       0.0s
Error response from daemon: driver failed programming external connectivity on endpoint dev-controller-1 (608529395aa4632b0e92a5b8af35dcb38f033207cc15a1875be1d9d5f972f738): Error starting userland proxy: listen tcp4 0.0.0.0:9108: bind: address already in use
make[2]: *** [Makefile:90: labs-aio-up] Error 1
make[2]: Leaving directory '/home/m4xx3d0ut/git/k1s'
make[1]: *** [Makefile:228: bench-mem-e2e-k1nd] Error 2
make[1]: Leaving directory '/home/m4xx3d0ut/git/k1s'
make: *** [Makefile:259: bench-mem-e2e-baselines-sudo] Error 2
