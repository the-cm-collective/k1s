[init-demo] Attaching logs (Ctrl-C to exit)
[controller] 2025-10-24 18:42:20 WARNING __main__: watchdog not available; falling back to interval polling
[controller] 2025-10-24 18:42:20 INFO ae.ingress.caddy: Caddy container dev-caddy-1 not available yet; skipping reload
[controller] 2025-10-24 18:42:21 INFO ae.ingress.caddy: Caddy container dev-caddy-1 not available yet; skipping reload
[controller] 2025-10-24 18:42:21 INFO ae.ingress.caddy: Caddy container dev-caddy-1 not available yet; skipping reload
[controller] 2025-10-24 18:42:21 INFO ae.ingress.caddy: Caddy container dev-caddy-1 not available yet; skipping reload
[controller] 2025-10-24 18:42:22 INFO ae.ingress.caddy: Caddy container dev-caddy-1 not available yet; skipping reload
[controller] Traceback (most recent call last):
[controller]   File "/home/m4xx3d0ut/git/k1s/src/ae/secrets/manager.py", line 56, in _decrypt
[controller]     completed = subprocess.run(  # noqa: S603
[controller]         [self._sops, "--decrypt", str(path)],
[controller]     ...<2 lines>...
[controller]         text=True,
[controller]     )
[controller]   File "/usr/lib/python3.13/subprocess.py", line 577, in run
[controller]     raise CalledProcessError(retcode, process.args,
[controller]                              output=stdout, stderr=stderr)
[controller] subprocess.CalledProcessError: Command '['sops', '--decrypt', 'specs/examples/demo-secret.sops.yaml']' returned non-zero exit status 1.
[controller]
[controller] The above exception was the direct cause of the following exception:
[controller]
[controller] Traceback (most recent call last):
[controller]   File "<frozen runpy>", line 198, in _run_module_as_main
[controller]   File "<frozen runpy>", line 88, in _run_code
[controller]   File "/home/m4xx3d0ut/git/k1s/src/ae/controller/__main__.py", line 268, in <module>
[controller]     raise SystemExit(main())
[controller]                      ~~~~^^
[controller]   File "/home/m4xx3d0ut/git/k1s/src/ae/controller/__main__.py", line 243, in main
[controller]     _reconcile_all(reconciler, manifests)
[controller]     ~~~~~~~~~~~~~~^^^^^^^^^^^^^^^^^^^^^^^
[controller]   File "/home/m4xx3d0ut/git/k1s/src/ae/controller/__main__.py", line 90, in _reconcile_all
[controller]     report = reconciler.reconcile(m)
[controller]   File "/home/m4xx3d0ut/git/k1s/src/ae/controller/reconciler.py", line 73, in reconcile
[controller]     manifest_with_env = self._apply_configs_and_secrets(manifest)
[controller]   File "/home/m4xx3d0ut/git/k1s/src/ae/controller/reconciler.py", line 215, in _apply_configs_and_secrets
[controller]     sec_env = self._secret_manager.load_env(manifest.spec.secret_refs)
[controller]   File "/home/m4xx3d0ut/git/k1s/src/ae/secrets/manager.py", line 36, in load_env
[controller]     decrypted = self._decrypt(Path(ref.path))
[controller]   File "/home/m4xx3d0ut/git/k1s/src/ae/secrets/manager.py", line 86, in _decrypt
[controller]     raise RuntimeError(f"sops decrypt failed for {path}: {exc.stderr}") from exc
[controller] RuntimeError: sops decrypt failed for specs/examples/demo-secret.sops.yaml: sops metadata not found
[controller]
[controller] Applied blue: +1/~0/-0, ready=1, live=1, rev=3(ready)
[controller] Applied echo: +1/~0/-0, ready=0, live=1, rev=253(progressing)
[controller] Applied echo-stateful: +1/~0/-0, ready=1, live=1, rev=1(ready)
[controller] Applied echo-del: +1/~0/-0, ready=1, live=1, rev=1(ready)
[controller] Applied echo: +1/~0/-0, ready=1, live=1, rev=254(ready)
[sites] ==> state/caddy/api.caddy <==
[sites]
[controller] 2025-10-24 18:43:16 INFO __main__: http api listening on port 9108
[sites] ==> state/caddy/blue.caddy <==
[sites]
[sites] ==> state/caddy/docs.caddy <==
[sites]
[controller] 2025-10-24 18:43:16 INFO __main__: watchdog not available; falling back to interval polling
[sites] ==> state/caddy/echo-mr.caddy <==
[sites]
[sites] ==> state/caddy/green.caddy <==
[controller] 2025-10-24 18:43:16 INFO __main__: http api listening on port 37357
[controller] 2025-10-24 18:43:16 INFO __main__: watchdog not available; falling back to interval polling
[caddy] {"level":"info","ts":1761356596.4720101,"msg":"using config from file","file":"/etc/caddy/Caddyfile"}
[caddy] {"level":"warn","ts":1761356596.4720619,"msg":"No files matching import glob pattern","pattern":"sites/*"}
[caddy] {"level":"info","ts":1761356596.4728606,"msg":"adapted config to JSON","adapter":"caddyfile"}
[caddy] {"level":"warn","ts":1761356596.4728658,"msg":"Caddyfile input is not formatted; run 'caddy fmt --overwrite' to fix inconsistencies","adapter":"caddyfile","file":"/etc/caddy/Caddyfile","line":3}
[caddy] {"level":"info","ts":1761356596.4734285,"logger":"admin","msg":"admin endpoint started","address":"localhost:2019","enforce_origin":false,"origins":["//localhost:2019","//[::1]:2019","//127.0.0.1:2019"]}
[caddy] {"level":"info","ts":1761356596.473527,"logger":"http.auto_https","msg":"server is listening only on the HTTPS port but has no TLS connection policies; adding one to enable TLS","server_name":"srv0","https_port":443}
[caddy] {"level":"info","ts":1761356596.4735348,"logger":"http.auto_https","msg":"automatic HTTP->HTTPS redirects are disabled","server_name":"srv0"}
[caddy] {"level":"info","ts":1761356596.4735844,"logger":"tls.cache.maintenance","msg":"started background certificate maintenance","cache":"0xc00060fd80"}
[caddy] {"level":"info","ts":1761356596.4773927,"logger":"http","msg":"enabling HTTP/3 listener","addr":":443"}
[caddy] {"level":"info","ts":1761356596.4774373,"msg":"failed to sufficiently increase receive buffer size (was: 208 kiB, wanted: 7168 kiB, got: 416 kiB). See https://github.com/quic-go/quic-go/wiki/UDP-Buffer-Sizes for details."}
[caddy] {"level":"info","ts":1761356596.477491,"logger":"http.log","msg":"server running","name":"srv0","protocols":["h1","h2","h3"]}
[caddy] {"level":"info","ts":1761356596.4774945,"logger":"http","msg":"enabling automatic TLS certificate management","domains":["echo-mr.home.arpa","green.home.arpa","blue.home.arpa","docs.home.arpa","api.home.arpa"]}
[caddy] {"level":"info","ts":1761356596.4779189,"logger":"tls.obtain","msg":"acquiring lock","identifier":"echo-mr.home.arpa"}
[caddy] {"level":"warn","ts":1761356596.478122,"logger":"pki.ca.local","msg":"installing root certificate (you might be prompted for password)","path":"storage:pki/authorities/local/root.crt"}
[caddy] {"level":"info","ts":1761356596.4785118,"logger":"tls.obtain","msg":"acquiring lock","identifier":"docs.home.arpa"}
[caddy] {"level":"info","ts":1761356596.4785616,"logger":"tls.obtain","msg":"acquiring lock","identifier":"blue.home.arpa"}
[caddy] {"level":"info","ts":1761356596.4786434,"logger":"tls.obtain","msg":"acquiring lock","identifier":"api.home.arpa"}
[caddy] {"level":"info","ts":1761356596.478689,"logger":"tls.obtain","msg":"acquiring lock","identifier":"green.home.arpa"}
[caddy] {"level":"info","ts":1761356596.4793453,"msg":"warning: \"certutil\" is not available, install \"certutil\" with \"apt install libnss3-tools\" or \"yum install nss-tools\" and try again"}
[caddy] {"level":"info","ts":1761356596.4793515,"msg":"define JAVA_HOME environment variable to use the Java trust"}
[caddy] {"level":"info","ts":1761356596.480159,"logger":"tls.obtain","msg":"lock acquired","identifier":"blue.home.arpa"}
[caddy] {"level":"info","ts":1761356596.4801614,"logger":"tls.obtain","msg":"lock acquired","identifier":"api.home.arpa"}
[caddy] {"level":"info","ts":1761356596.4801643,"logger":"tls.obtain","msg":"lock acquired","identifier":"echo-mr.home.arpa"}
[caddy] {"level":"info","ts":1761356596.4801636,"logger":"tls.obtain","msg":"lock acquired","identifier":"green.home.arpa"}
[caddy] {"level":"info","ts":1761356596.4801714,"logger":"tls","msg":"cleaning storage unit","storage":"FileStorage:/data/caddy"}
[caddy] {"level":"info","ts":1761356596.4801626,"logger":"tls.obtain","msg":"lock acquired","identifier":"docs.home.arpa"}
[caddy] {"level":"info","ts":1761356596.4802024,"logger":"tls.obtain","msg":"obtaining certificate","identifier":"api.home.arpa"}
[caddy] {"level":"info","ts":1761356596.4802082,"logger":"tls.obtain","msg":"obtaining certificate","identifier":"echo-mr.home.arpa"}
[caddy] {"level":"info","ts":1761356596.4802136,"logger":"tls.obtain","msg":"obtaining certificate","identifier":"docs.home.arpa"}
[caddy] {"level":"info","ts":1761356596.4802215,"logger":"tls.obtain","msg":"obtaining certificate","identifier":"green.home.arpa"}
[caddy] {"level":"info","ts":1761356596.4802845,"logger":"tls.obtain","msg":"obtaining certificate","identifier":"blue.home.arpa"}
[caddy] {"level":"info","ts":1761356596.4803772,"logger":"tls","msg":"finished cleaning storage units"}
[caddy] {"level":"info","ts":1761356596.480825,"logger":"tls.obtain","msg":"certificate obtained successfully","identifier":"blue.home.arpa","issuer":"local"}
[caddy] {"level":"info","ts":1761356596.4808555,"logger":"tls.obtain","msg":"releasing lock","identifier":"blue.home.arpa"}
[caddy] {"level":"warn","ts":1761356596.4810026,"logger":"tls","msg":"stapling OCSP","error":"no OCSP stapling for [blue.home.arpa]: no OCSP server specified in certificate","identifiers":["blue.home.arpa"]}
[prometheus] ts=2025-10-25T01:43:16.450Z caller=main.go:589 level=info msg="No time or size retention was set so using the default time retention" duration=15d
[caddy] {"level":"info","ts":1761356596.4812176,"logger":"tls.obtain","msg":"certificate obtained successfully","identifier":"api.home.arpa","issuer":"local"}
[caddy] {"level":"info","ts":1761356596.4812684,"logger":"tls.obtain","msg":"releasing lock","identifier":"api.home.arpa"}
[prometheus] ts=2025-10-25T01:43:16.450Z caller=main.go:633 level=info msg="Starting Prometheus Server" mode=server version="(version=2.53.0, branch=HEAD, revision=4c35b9250afefede41c5f5acd76191f90f625898)"
[caddy] {"level":"info","ts":1761356596.48122,"logger":"tls.obtain","msg":"certificate obtained successfully","identifier":"echo-mr.home.arpa","issuer":"local"}
[prometheus] ts=2025-10-25T01:43:16.450Z caller=main.go:638 level=info build_context="(go=go1.22.4, platform=linux/amd64, user=root@7f8d89cbbd64, date=20240619-07:39:12, tags=netgo,builtinassets,stringlabels)"
[caddy] {"level":"info","ts":1761356596.4812896,"logger":"tls.obtain","msg":"certificate obtained successfully","identifier":"docs.home.arpa","issuer":"local"}
[prometheus] ts=2025-10-25T01:43:16.450Z caller=main.go:639 level=info host_details="(Linux 6.12.38+kali-amd64 #1 SMP PREEMPT_DYNAMIC Kali 6.12.38-1kali1 (2025-08-12) x86_64 26212371cbca (none))"
[caddy] {"level":"info","ts":1761356596.4813187,"logger":"tls.obtain","msg":"certificate obtained successfully","identifier":"green.home.arpa","issuer":"local"}
[prometheus] ts=2025-10-25T01:43:16.450Z caller=main.go:640 level=info fd_limits="(soft=524288, hard=524288)"
[caddy] {"level":"info","ts":1761356596.4813445,"logger":"tls.obtain","msg":"releasing lock","identifier":"echo-mr.home.arpa"}
[prometheus] ts=2025-10-25T01:43:16.450Z caller=main.go:641 level=info vm_limits="(soft=unlimited, hard=unlimited)"
[caddy] {"level":"info","ts":1761356596.4813805,"logger":"tls.obtain","msg":"releasing lock","identifier":"green.home.arpa"}
[prometheus] ts=2025-10-25T01:43:16.452Z caller=web.go:568 level=info component=web msg="Start listening for connections" address=0.0.0.0:9090
[caddy] {"level":"info","ts":1761356596.4813442,"logger":"tls.obtain","msg":"releasing lock","identifier":"docs.home.arpa"}
[prometheus] ts=2025-10-25T01:43:16.452Z caller=main.go:1148 level=info msg="Starting TSDB ..."
[prometheus] ts=2025-10-25T01:43:16.452Z caller=tls_config.go:313 level=info component=web msg="Listening on" address=[::]:9090
[caddy] {"level":"warn","ts":1761356596.4814682,"logger":"tls","msg":"stapling OCSP","error":"no OCSP stapling for [echo-mr.home.arpa]: no OCSP server specified in certificate","identifiers":["echo-mr.home.arpa"]}
[prometheus] ts=2025-10-25T01:43:16.452Z caller=tls_config.go:316 level=info component=web msg="TLS is disabled." http2=false address=[::]:9090
[caddy] {"level":"warn","ts":1761356596.4814823,"logger":"tls","msg":"stapling OCSP","error":"no OCSP stapling for [api.home.arpa]: no OCSP server specified in certificate","identifiers":["api.home.arpa"]}
[prometheus] ts=2025-10-25T01:43:16.454Z caller=head.go:626 level=info component=tsdb msg="Replaying on-disk memory mappable chunks if any"
[prometheus] ts=2025-10-25T01:43:16.454Z caller=head.go:713 level=info component=tsdb msg="On-disk memory mappable chunks replay completed" duration=1.232µs
[caddy] {"level":"warn","ts":1761356596.4815845,"logger":"tls","msg":"stapling OCSP","error":"no OCSP stapling for [green.home.arpa]: no OCSP server specified in certificate","identifiers":["green.home.arpa"]}
[prometheus] ts=2025-10-25T01:43:16.454Z caller=head.go:721 level=info component=tsdb msg="Replaying WAL, this may take a while"
[caddy] {"level":"warn","ts":1761356596.481598,"logger":"tls","msg":"stapling OCSP","error":"no OCSP stapling for [docs.home.arpa]: no OCSP server specified in certificate","identifiers":["docs.home.arpa"]}
[prometheus] ts=2025-10-25T01:43:16.455Z caller=head.go:793 level=info component=tsdb msg="WAL segment loaded" segment=0 maxSegment=0
[caddy] {"level":"info","ts":1761356596.506582,"msg":"certificate installed properly in linux trusts"}
[caddy] {"level":"info","ts":1761356596.508645,"msg":"autosaved config (load with --resume flag)","file":"/config/caddy/autosave.json"}
[caddy] {"level":"info","ts":1761356596.5086648,"msg":"serving initial configuration"}
[prometheus] ts=2025-10-25T01:43:16.455Z caller=head.go:830 level=info component=tsdb msg="WAL replay completed" checkpoint_replay_duration=14.771µs wal_replay_duration=201.824µs wbl_replay_duration=175ns chunk_snapshot_load_duration=0s mmap_chunk_replay_duration=1.232µs total_replay_duration=228.191µs
[prometheus] ts=2025-10-25T01:43:16.455Z caller=main.go:1169 level=info fs_type=EXT4_SUPER_MAGIC
[prometheus] ts=2025-10-25T01:43:16.455Z caller=main.go:1172 level=info msg="TSDB started"
[caddy] {"level":"info","ts":1761356596.6119177,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"44796","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[prometheus] ts=2025-10-25T01:43:16.455Z caller=main.go:1354 level=info msg="Loading configuration file" filename=/etc/prometheus/prometheus.yml
[caddy] {"level":"info","ts":1761356596.6120965,"msg":"config is unchanged"}
[prometheus] ts=2025-10-25T01:43:16.456Z caller=main.go:1391 level=info msg="updated GOGC" old=100 new=75
[caddy] {"level":"info","ts":1761356596.612104,"logger":"admin.api","msg":"load complete"}
[prometheus] ts=2025-10-25T01:43:16.456Z caller=main.go:1402 level=info msg="Completed loading of configuration file" filename=/etc/prometheus/prometheus.yml totalDuration=327.747µs db_storage=1.58µs remote_storage=1.516µs web_handler=367ns query_engine=930ns scrape=140.229µs scrape_sd=14.197µs notify=782ns notify_sd=448ns rules=2.168µs tracing=4.765µs
[caddy] {"level":"info","ts":1761356596.881814,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"44810","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356596.8819659,"msg":"config is unchanged"}
[prometheus] ts=2025-10-25T01:43:16.456Z caller=main.go:1133 level=info msg="Server is ready to receive web requests."
[caddy] {"level":"info","ts":1761356596.8819706,"logger":"admin.api","msg":"load complete"}
[prometheus] ts=2025-10-25T01:43:16.456Z caller=manager.go:164 level=info component="rule manager" msg="Starting rule manager..."
[caddy] {"level":"info","ts":1761356596.9740574,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"44824","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356596.9741604,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356596.974163,"logger":"admin.api","msg":"load complete"}
[caddy] {"level":"info","ts":1761356597.4766757,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"44826","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356597.4768054,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356597.4768076,"logger":"admin.api","msg":"load complete"}
[caddy] 2025/10/25 01:43:17.748 INFO    http.log.access.log0    handled request{"request": {"remote_ip": "172.26.0.1", "remote_port": "54048", "client_ip": "172.26.0.1", "proto": "HTTP/2.0", "method": "GET", "host": "api.home.arpa:8443", "uri": "/openapi.json", "headers": {"User-Agent": ["curl/8.15.0"], "Accept": ["*/*"]}, "tls": {"resumed": false, "version": 772, "cipher_suite": 4865, "proto": "h2", "server_name": "api.home.arpa"}}, "bytes_read": 0, "user_id": "", "duration": 0.000681612, "size": 3393, "status": 200, "resp_headers": {"Server": ["Caddy", "BaseHTTP/0.6 Python/3.13.7"], "Alt-Svc": ["h3=\":443\"; ma=2592000"], "Content-Length": ["3393"], "Date": ["Sat, 25 Oct 2025 01:43:17 GMT"], "Content-Type": ["application/json"]}}
[controller] 2025-10-24 18:43:28 WARNING ae.runtime.docker_runtime: Failed to remove container ae-echo-rev256-0: 409 Client Error for http+docker://localhost/v1.45/containers/4bca164896680347c74bbca1bf0fcfc7e84a480582d6b016334e99d4a62db274?v=False&link=False&force=False: Conflict ("removal of container 4bca164896680347c74bbca1bf0fcfc7e84a480582d6b016334e99d4a62db274 is already in progress")
[controller] 2025-10-24 18:43:38 WARNING ae.runtime.docker_runtime: Failed to remove container ae-echo-rev254-0: 409 Client Error for http+docker://localhost/v1.45/containers/c5d191a067a42c57e5418a2007299f8e6775575dc30aecbc9d43d9893782b466?v=False&link=False&force=False: Conflict ("removal of container c5d191a067a42c57e5418a2007299f8e6775575dc30aecbc9d43d9893782b466 is already in progress")
[caddy] {"level":"info","ts":1761356618.9940853,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"45646","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356618.9942074,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356618.9942095,"logger":"admin.api","msg":"load complete"}
[sites]
[sites] ==> state/caddy/echo-mr.caddy <==
[sites] https://echo-mr.home.arpa {
[sites]     log {
[sites]         output stdout
[sites]         format console
[sites]     }
[sites]     # Ensure upstream HSTS does not stick during dev
[sites]     header -Strict-Transport-Security
[sites]     reverse_proxy host.docker.internal:33294 host.docker.internal:33293 host.docker.internal:33292 {
[sites]
[sites]         lb_policy first
[sites]     }
[sites] }
[caddy] {"level":"info","ts":1761356619.2443662,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"45662","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356619.2451053,"logger":"admin","msg":"admin endpoint started","address":"localhost:2019","enforce_origin":false,"origins":["//[::1]:2019","//127.0.0.1:2019","//localhost:2019"]}
[caddy] {"level":"info","ts":1761356619.245238,"logger":"http.auto_https","msg":"server is listening only on the HTTPS port but has no TLS connection policies; adding one to enable TLS","server_name":"srv0","https_port":443}
[caddy] {"level":"info","ts":1761356619.245259,"logger":"http.auto_https","msg":"automatic HTTP->HTTPS redirects are disabled","server_name":"srv0"}
[caddy] {"level":"warn","ts":1761356619.2458036,"logger":"pki.ca.local","msg":"installing root certificate (you might be prompted for password)","path":"storage:pki/authorities/local/root.crt"}
[caddy] {"level":"info","ts":1761356619.2459323,"msg":"warning: \"certutil\" is not available, install \"certutil\" with \"apt install libnss3-tools\" or \"yum install nss-tools\" and try again"}
[caddy] {"level":"info","ts":1761356619.2459385,"msg":"define JAVA_HOME environment variable to use the Java trust"}
[caddy] {"level":"info","ts":1761356619.2640254,"msg":"certificate installed properly in linux trusts"}
[caddy] {"level":"info","ts":1761356619.2643113,"logger":"http","msg":"enabling HTTP/3 listener","addr":":443"}
[caddy] {"level":"info","ts":1761356619.2643416,"logger":"http.log","msg":"server running","name":"srv0","protocols":["h1","h2","h3"]}
[caddy] {"level":"info","ts":1761356619.2643478,"logger":"http","msg":"enabling automatic TLS certificate management","domains":["green.home.arpa","blue.home.arpa","docs.home.arpa","api.home.arpa","echo-mr.home.arpa"]}
[caddy] {"level":"info","ts":1761356619.2643716,"logger":"http","msg":"servers shutting down with eternal grace period"}
[caddy] {"level":"info","ts":1761356619.2646735,"msg":"autosaved config (load with --resume flag)","file":"/config/caddy/autosave.json"}
[caddy] {"level":"info","ts":1761356619.264704,"logger":"admin.api","msg":"load complete"}
[controller] Traceback (most recent call last):
[controller]   File "/home/m4xx3d0ut/git/k1s/src/ae/secrets/manager.py", line 56, in _decrypt
[controller]     completed = subprocess.run(  # noqa: S603
[controller]         [self._sops, "--decrypt", str(path)],
[controller]     ...<2 lines>...
[controller]         text=True,
[controller]     )
[controller]   File "/usr/lib/python3.13/subprocess.py", line 577, in run
[controller]     raise CalledProcessError(retcode, process.args,
[controller]                              output=stdout, stderr=stderr)
[controller] subprocess.CalledProcessError: Command '['sops', '--decrypt', 'specs/examples/demo-secret.sops.yaml']' returned non-zero exit status 1.
[controller]
[controller] The above exception was the direct cause of the following exception:
[controller]
[controller] Traceback (most recent call last):
[controller]   File "<frozen runpy>", line 198, in _run_module_as_main
[controller]   File "<frozen runpy>", line 88, in _run_code
[controller]   File "/home/m4xx3d0ut/git/k1s/src/ae/controller/__main__.py", line 268, in <module>
[controller]     raise SystemExit(main())
[controller]                      ~~~~^^
[controller]   File "/home/m4xx3d0ut/git/k1s/src/ae/controller/__main__.py", line 243, in main
[controller]     _reconcile_all(reconciler, manifests)
[controller]     ~~~~~~~~~~~~~~^^^^^^^^^^^^^^^^^^^^^^^
[controller]   File "/home/m4xx3d0ut/git/k1s/src/ae/controller/__main__.py", line 90, in _reconcile_all
[controller]     report = reconciler.reconcile(m)
[controller]   File "/home/m4xx3d0ut/git/k1s/src/ae/controller/reconciler.py", line 73, in reconcile
[controller]     manifest_with_env = self._apply_configs_and_secrets(manifest)
[controller]   File "/home/m4xx3d0ut/git/k1s/src/ae/controller/reconciler.py", line 215, in _apply_configs_and_secrets
[controller]     sec_env = self._secret_manager.load_env(manifest.spec.secret_refs)
[controller]   File "/home/m4xx3d0ut/git/k1s/src/ae/secrets/manager.py", line 36, in load_env
[controller]     decrypted = self._decrypt(Path(ref.path))
[controller]   File "/home/m4xx3d0ut/git/k1s/src/ae/secrets/manager.py", line 86, in _decrypt
[controller]     raise RuntimeError(f"sops decrypt failed for {path}: {exc.stderr}") from exc
[controller] RuntimeError: sops decrypt failed for specs/examples/demo-secret.sops.yaml: sops metadata not found
[controller]
[controller] Applied blue: +0/~0/-0, ready=1, live=1, rev=3(ready)
[controller] Applied echo: +1/~0/-0, ready=0, live=1, rev=256(progressing)
[controller] Applied echo-stateful: +0/~0/-0, ready=1, live=1, rev=1(ready)
[controller] Applied echo-del: +0/~0/-0, ready=1, live=1, rev=1(ready)
[controller] Applied echo: +1/~0/-0, ready=1, live=1, rev=257(ready)
[caddy] {"level":"info","ts":1761356619.2806094,"logger":"admin","msg":"stopped previous server","address":"localhost:2019"}
