[init-demo] Building static docs (docs/site)
[init-demo] Starting docs server on http://127.0.0.1:9109 (background)
[init-demo] Current status
blue: desired=1, ready=1, live=1, rev=3(ready), image=demo-blue:latest, ops=+1/~0/-0, ingress=blue.home.arpa/
echo: desired=1, ready=0, live=1, rev=202(progressing), image=alpine:3.20, ops=+1/~0/-0
echo-del: desired=1, ready=1, live=1, rev=1(ready), image=alpine:3.20, ops=+0/~0/-0
echo-mr: desired=3, ready=0, live=3, rev=2(progressing), image=demo-blue:latest, ops=+3/~0/-0, ingress=echo-mr.home.arpa/
echo-stateful: desired=1, ready=1, live=1, rev=1(ready), image=alpine:3.20, ops=+0/~0/-0
green: desired=1, ready=1, live=1, rev=3(ready), image=demo-green:latest, ops=+1/~0/-0, ingress=green.home.arpa/

[init-demo] API reachability checks (expected after you start the controller)
[init-demo] Direct API OK: http://127.0.0.1:9108/openapi.json
[init-demo] Direct /swagger OK: http://127.0.0.1:9108/swagger
[init-demo] Direct /redoc OK: http://127.0.0.1:9108/redoc
[init-demo] Caddy API OK: https://api.home.arpa:8443/openapi.json
[init-demo] Controller supervisor running; restarts=0, last_exit=0

[init-demo] Ingress/network sanity checks
[init-demo] [api] upstream targets detected: 1, basic checks passed: 1
[init-demo] [blue] upstream targets detected: 1, basic checks passed: 1
[init-demo] [docs] upstream targets detected: 1, basic checks passed: 1
[init-demo] [echo-mr] upstream targets detected: 3, basic checks passed: 3
[init-demo] [green] upstream targets detected: 1, basic checks passed: 1

[init-demo] Attaching logs (Ctrl-C to exit)
[controller]
[controller] Applied blue: +0/~0/-0, ready=1, live=1, rev=3(ready)
[controller] Applied echo: +1/~0/-0, ready=0, live=1, rev=196(progressing)
[controller] Applied echo-stateful: +0/~0/-0, ready=1, live=1, rev=1(ready)
[controller] Applied echo-del: +0/~0/-0, ready=1, live=1, rev=1(ready)
[controller] Applied echo: +1/~0/-0, ready=1, live=1, rev=197(ready)
[controller] 2025-10-24 18:34:47 INFO __main__: http api listening on port 9108
[controller] 2025-10-24 18:34:47 WARNING __main__: watchdog not available; falling back to interval polling
[controller] Traceback (most recent call last):
[controller]   File "/home/m4xx3d0ut/git/k1s/src/ae/secrets/manager.py", line 50, in _decrypt
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
[controller]   File "/home/m4xx3d0ut/git/k1s/src/ae/secrets/manager.py", line 65, in _decrypt
[controller]     raise RuntimeError(f"sops decrypt failed for {path}: {exc.stderr}") from exc
[controller] RuntimeError: sops decrypt failed for specs/examples/demo-secret.sops.yaml: sops metadata not found
[controller]
[controller] Applied blue: +0/~0/-0, ready=1, live=1, rev=3(ready)
[controller] Applied echo: +1/~0/-0, ready=0, live=1, rev=199(progressing)
[controller] Applied echo-stateful: +0/~0/-0, ready=1, live=1, rev=1(ready)
[controller] Applied echo-del: +0/~0/-0, ready=1, live=1, rev=1(ready)
[controller] Applied echo: +1/~0/-0, ready=1, live=1, rev=200(ready)
[controller] 2025-10-24 18:35:29 INFO __main__: http api listening on port 9108
[controller] 2025-10-24 18:35:29 WARNING __main__: watchdog not available; falling back to interval polling
[sites] ==> state/caddy/api.caddy <==
[sites]
[sites] ==> state/caddy/blue.caddy <==
[sites]
[sites] ==> state/caddy/docs.caddy <==
[sites]
[sites] ==> state/caddy/echo-mr.caddy <==
[sites]
[sites] ==> state/caddy/green.caddy <==
[caddy] {"level":"info","ts":1761356129.182541,"msg":"using config from file","file":"/etc/caddy/Caddyfile"}
[caddy] {"level":"warn","ts":1761356129.182612,"msg":"No files matching import glob pattern","pattern":"sites/*"}
[caddy] {"level":"info","ts":1761356129.1837335,"msg":"adapted config to JSON","adapter":"caddyfile"}
[caddy] {"level":"warn","ts":1761356129.1837447,"msg":"Caddyfile input is not formatted; run 'caddy fmt --overwrite' to fix inconsistencies","adapter":"caddyfile","file":"/etc/caddy/Caddyfile","line":3}
[caddy] {"level":"info","ts":1761356129.1843147,"logger":"admin","msg":"admin endpoint started","address":"localhost:2019","enforce_origin":false,"origins":["//localhost:2019","//[::1]:2019","//127.0.0.1:2019"]}
[caddy] {"level":"info","ts":1761356129.1843917,"logger":"http.auto_https","msg":"server is listening only on the HTTPS port but has no TLS connection policies; adding one to enable TLS","server_name":"srv0","https_port":443}
[caddy] {"level":"info","ts":1761356129.1843991,"logger":"http.auto_https","msg":"automatic HTTP->HTTPS redirects are disabled","server_name":"srv0"}
[caddy] {"level":"info","ts":1761356129.184483,"logger":"tls.cache.maintenance","msg":"started background certificate maintenance","cache":"0xc000460900"}
[caddy] {"level":"warn","ts":1761356129.187393,"logger":"pki.ca.local","msg":"installing root certificate (you might be prompted for password)","path":"storage:pki/authorities/local/root.crt"}
[caddy] {"level":"info","ts":1761356129.1883159,"msg":"warning: \"certutil\" is not available, install \"certutil\" with \"apt install libnss3-tools\" or \"yum install nss-tools\" and try again"}
[caddy] {"level":"info","ts":1761356129.18832,"msg":"define JAVA_HOME environment variable to use the Java trust"}
[caddy] {"level":"info","ts":1761356129.209685,"msg":"certificate installed properly in linux trusts"}
[caddy] {"level":"info","ts":1761356129.209887,"logger":"http","msg":"enabling HTTP/3 listener","addr":":443"}
[caddy] {"level":"info","ts":1761356129.2099566,"msg":"failed to sufficiently increase receive buffer size (was: 208 kiB, wanted: 7168 kiB, got: 416 kiB). See https://github.com/quic-go/quic-go/wiki/UDP-Buffer-Sizes for details."}
[caddy] {"level":"info","ts":1761356129.2100532,"logger":"http.log","msg":"server running","name":"srv0","protocols":["h1","h2","h3"]}
[caddy] {"level":"info","ts":1761356129.210061,"logger":"http","msg":"enabling automatic TLS certificate management","domains":["api.home.arpa","echo-mr.home.arpa","green.home.arpa","blue.home.arpa","docs.home.arpa"]}
[caddy] {"level":"info","ts":1761356129.2105894,"logger":"tls.obtain","msg":"acquiring lock","identifier":"echo-mr.home.arpa"}
[caddy] {"level":"info","ts":1761356129.2106082,"logger":"tls.obtain","msg":"acquiring lock","identifier":"green.home.arpa"}
[caddy] {"level":"info","ts":1761356129.2107944,"logger":"tls.obtain","msg":"acquiring lock","identifier":"docs.home.arpa"}
[caddy] {"level":"info","ts":1761356129.210936,"logger":"tls.obtain","msg":"acquiring lock","identifier":"blue.home.arpa"}
[caddy] {"level":"info","ts":1761356129.2109728,"logger":"tls.obtain","msg":"acquiring lock","identifier":"api.home.arpa"}
[caddy] {"level":"info","ts":1761356129.212161,"logger":"tls.obtain","msg":"lock acquired","identifier":"docs.home.arpa"}
[caddy] {"level":"info","ts":1761356129.2121637,"logger":"tls.obtain","msg":"lock acquired","identifier":"blue.home.arpa"}
[caddy] {"level":"info","ts":1761356129.2121754,"logger":"tls","msg":"cleaning storage unit","storage":"FileStorage:/data/caddy"}
[caddy] {"level":"info","ts":1761356129.2122173,"logger":"tls.obtain","msg":"obtaining certificate","identifier":"docs.home.arpa"}
[caddy] {"level":"info","ts":1761356129.212161,"logger":"tls.obtain","msg":"lock acquired","identifier":"api.home.arpa"}
[caddy] {"level":"info","ts":1761356129.2122304,"logger":"tls.obtain","msg":"obtaining certificate","identifier":"blue.home.arpa"}
[caddy] {"level":"info","ts":1761356129.2122638,"logger":"tls.obtain","msg":"obtaining certificate","identifier":"api.home.arpa"}
[caddy] {"level":"info","ts":1761356129.2121637,"logger":"tls.obtain","msg":"lock acquired","identifier":"echo-mr.home.arpa"}
[caddy] {"level":"info","ts":1761356129.212167,"logger":"tls.obtain","msg":"lock acquired","identifier":"green.home.arpa"}
[caddy] {"level":"info","ts":1761356129.212315,"logger":"tls","msg":"finished cleaning storage units"}
[caddy] {"level":"info","ts":1761356129.2123206,"logger":"tls.obtain","msg":"obtaining certificate","identifier":"echo-mr.home.arpa"}
[caddy] {"level":"info","ts":1761356129.2123182,"logger":"tls.obtain","msg":"obtaining certificate","identifier":"green.home.arpa"}
[caddy] {"level":"info","ts":1761356129.2128193,"logger":"tls.obtain","msg":"certificate obtained successfully","identifier":"blue.home.arpa","issuer":"local"}
[caddy] {"level":"info","ts":1761356129.2128518,"logger":"tls.obtain","msg":"releasing lock","identifier":"blue.home.arpa"}
[caddy] {"level":"warn","ts":1761356129.2129812,"logger":"tls","msg":"stapling OCSP","error":"no OCSP stapling for [blue.home.arpa]: no OCSP server specified in certificate","identifiers":["blue.home.arpa"]}
[caddy] {"level":"info","ts":1761356129.2130008,"msg":"autosaved config (load with --resume flag)","file":"/config/caddy/autosave.json"}
[caddy] {"level":"info","ts":1761356129.21301,"msg":"serving initial configuration"}
[caddy] {"level":"info","ts":1761356129.2130568,"logger":"tls.obtain","msg":"certificate obtained successfully","identifier":"api.home.arpa","issuer":"local"}
[caddy] {"level":"info","ts":1761356129.213114,"logger":"tls.obtain","msg":"releasing lock","identifier":"api.home.arpa"}
[caddy] {"level":"info","ts":1761356129.2131252,"logger":"tls.obtain","msg":"certificate obtained successfully","identifier":"docs.home.arpa","issuer":"local"}
[caddy] {"level":"info","ts":1761356129.2131512,"logger":"tls.obtain","msg":"certificate obtained successfully","identifier":"echo-mr.home.arpa","issuer":"local"}
[caddy] {"level":"info","ts":1761356129.2131777,"logger":"tls.obtain","msg":"releasing lock","identifier":"docs.home.arpa"}
[caddy] {"level":"info","ts":1761356129.213196,"logger":"tls.obtain","msg":"certificate obtained successfully","identifier":"green.home.arpa","issuer":"local"}
[prometheus] ts=2025-10-25T01:35:29.162Z caller=main.go:589 level=info msg="No time or size retention was set so using the default time retention" duration=15d
[caddy] {"level":"info","ts":1761356129.2132003,"logger":"tls.obtain","msg":"releasing lock","identifier":"echo-mr.home.arpa"}
[caddy] {"level":"info","ts":1761356129.213265,"logger":"tls.obtain","msg":"releasing lock","identifier":"green.home.arpa"}
[prometheus] ts=2025-10-25T01:35:29.162Z caller=main.go:633 level=info msg="Starting Prometheus Server" mode=server version="(version=2.53.0, branch=HEAD, revision=4c35b9250afefede41c5f5acd76191f90f625898)"
[caddy] {"level":"warn","ts":1761356129.2133267,"logger":"tls","msg":"stapling OCSP","error":"no OCSP stapling for [api.home.arpa]: no OCSP server specified in certificate","identifiers":["api.home.arpa"]}
[prometheus] ts=2025-10-25T01:35:29.162Z caller=main.go:638 level=info build_context="(go=go1.22.4, platform=linux/amd64, user=root@7f8d89cbbd64, date=20240619-07:39:12, tags=netgo,builtinassets,stringlabels)"
[caddy] {"level":"warn","ts":1761356129.213385,"logger":"tls","msg":"stapling OCSP","error":"no OCSP stapling for [docs.home.arpa]: no OCSP server specified in certificate","identifiers":["docs.home.arpa"]}
[prometheus] ts=2025-10-25T01:35:29.162Z caller=main.go:639 level=info host_details="(Linux 6.12.38+kali-amd64 #1 SMP PREEMPT_DYNAMIC Kali 6.12.38-1kali1 (2025-08-12) x86_64 d8f4c8888375 (none))"
[prometheus] ts=2025-10-25T01:35:29.162Z caller=main.go:640 level=info fd_limits="(soft=524288, hard=524288)"
[caddy] {"level":"warn","ts":1761356129.2133985,"logger":"tls","msg":"stapling OCSP","error":"no OCSP stapling for [echo-mr.home.arpa]: no OCSP server specified in certificate","identifiers":["echo-mr.home.arpa"]}
[prometheus] ts=2025-10-25T01:35:29.162Z caller=main.go:641 level=info vm_limits="(soft=unlimited, hard=unlimited)"
[caddy] {"level":"warn","ts":1761356129.213461,"logger":"tls","msg":"stapling OCSP","error":"no OCSP stapling for [green.home.arpa]: no OCSP server specified in certificate","identifiers":["green.home.arpa"]}
[prometheus] ts=2025-10-25T01:35:29.164Z caller=web.go:568 level=info component=web msg="Start listening for connections" address=0.0.0.0:9090
[prometheus] ts=2025-10-25T01:35:29.164Z caller=main.go:1148 level=info msg="Starting TSDB ..."
[prometheus] ts=2025-10-25T01:35:29.167Z caller=tls_config.go:313 level=info component=web msg="Listening on" address=[::]:9090
[prometheus] ts=2025-10-25T01:35:29.167Z caller=tls_config.go:316 level=info component=web msg="TLS is disabled." http2=false address=[::]:9090
[caddy] {"level":"info","ts":1761356129.8703947,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"47638","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[prometheus] ts=2025-10-25T01:35:29.168Z caller=head.go:626 level=info component=tsdb msg="Replaying on-disk memory mappable chunks if any"
[caddy] {"level":"info","ts":1761356129.8708615,"logger":"admin","msg":"admin endpoint started","address":"localhost:2019","enforce_origin":false,"origins":["//localhost:2019","//[::1]:2019","//127.0.0.1:2019"]}
[prometheus] ts=2025-10-25T01:35:29.168Z caller=head.go:713 level=info component=tsdb msg="On-disk memory mappable chunks replay completed" duration=1.91µs
[prometheus] ts=2025-10-25T01:35:29.168Z caller=head.go:721 level=info component=tsdb msg="Replaying WAL, this may take a while"
[caddy] {"level":"info","ts":1761356129.8709366,"logger":"http.auto_https","msg":"server is listening only on the HTTPS port but has no TLS connection policies; adding one to enable TLS","server_name":"srv0","https_port":443}
[prometheus] ts=2025-10-25T01:35:29.168Z caller=head.go:793 level=info component=tsdb msg="WAL segment loaded" segment=0 maxSegment=0
[caddy] {"level":"info","ts":1761356129.8709507,"logger":"http.auto_https","msg":"automatic HTTP->HTTPS redirects are disabled","server_name":"srv0"}
[prometheus] ts=2025-10-25T01:35:29.168Z caller=head.go:830 level=info component=tsdb msg="WAL replay completed" checkpoint_replay_duration=26.453µs wal_replay_duration=470.216µs wbl_replay_duration=207ns chunk_snapshot_load_duration=0s mmap_chunk_replay_duration=1.91µs total_replay_duration=515.926µs
[prometheus] ts=2025-10-25T01:35:29.170Z caller=main.go:1169 level=info fs_type=EXT4_SUPER_MAGIC
[caddy] {"level":"info","ts":1761356129.8711076,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"47640","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[prometheus] ts=2025-10-25T01:35:29.170Z caller=main.go:1172 level=info msg="TSDB started"
[prometheus] ts=2025-10-25T01:35:29.170Z caller=main.go:1354 level=info msg="Loading configuration file" filename=/etc/prometheus/prometheus.yml
[caddy] {"level":"warn","ts":1761356129.8712919,"logger":"pki.ca.local","msg":"installing root certificate (you might be prompted for password)","path":"storage:pki/authorities/local/root.crt"}
[prometheus] ts=2025-10-25T01:35:29.171Z caller=main.go:1391 level=info msg="updated GOGC" old=100 new=75
[caddy] {"level":"info","ts":1761356129.8713794,"msg":"warning: \"certutil\" is not available, install \"certutil\" with \"apt install libnss3-tools\" or \"yum install nss-tools\" and try again"}
[caddy] {"level":"info","ts":1761356129.871383,"msg":"define JAVA_HOME environment variable to use the Java trust"}
[caddy] {"level":"info","ts":1761356129.8917837,"msg":"certificate installed properly in linux trusts"}
[prometheus] ts=2025-10-25T01:35:29.171Z caller=main.go:1402 level=info msg="Completed loading of configuration file" filename=/etc/prometheus/prometheus.yml totalDuration=558.744µs db_storage=1.42µs remote_storage=1.984µs web_handler=425ns query_engine=1.05µs scrape=286.165µs scrape_sd=24.475µs notify=871ns notify_sd=952ns rules=1.875µs tracing=7.653µs
[caddy] {"level":"info","ts":1761356129.891873,"logger":"http","msg":"enabling HTTP/3 listener","addr":":443"}
[prometheus] ts=2025-10-25T01:35:29.171Z caller=main.go:1133 level=info msg="Server is ready to receive web requests."
[caddy] {"level":"info","ts":1761356129.891893,"logger":"http.log","msg":"server running","name":"srv0","protocols":["h1","h2","h3"]}
[prometheus] ts=2025-10-25T01:35:29.171Z caller=manager.go:164 level=info component="rule manager" msg="Starting rule manager..."
[caddy] {"level":"info","ts":1761356129.8918967,"logger":"http","msg":"enabling automatic TLS certificate management","domains":["blue.home.arpa","docs.home.arpa","api.home.arpa","echo-mr.home.arpa","green.home.arpa"]}
[caddy] {"level":"info","ts":1761356129.8919168,"logger":"http","msg":"servers shutting down with eternal grace period"}
[caddy] {"level":"info","ts":1761356129.8920171,"msg":"autosaved config (load with --resume flag)","file":"/config/caddy/autosave.json"}
[caddy] {"level":"info","ts":1761356129.8920212,"logger":"admin.api","msg":"load complete"}
[caddy] {"level":"info","ts":1761356129.892262,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356129.8922703,"logger":"admin.api","msg":"load complete"}
[caddy] {"level":"info","ts":1761356129.9071434,"logger":"admin","msg":"stopped previous server","address":"localhost:2019"}
[caddy] {"level":"info","ts":1761356130.4032001,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"33870","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356130.4033542,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356130.4033585,"logger":"admin.api","msg":"load complete"}
[caddy] {"level":"info","ts":1761356130.6528873,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"33880","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356130.6535864,"logger":"admin","msg":"admin endpoint started","address":"localhost:2019","enforce_origin":false,"origins":["//localhost:2019","//[::1]:2019","//127.0.0.1:2019"]}
[caddy] {"level":"info","ts":1761356130.6537607,"logger":"http.auto_https","msg":"server is listening only on the HTTPS port but has no TLS connection policies; adding one to enable TLS","server_name":"srv0","https_port":443}
[caddy] {"level":"info","ts":1761356130.6537802,"logger":"http.auto_https","msg":"automatic HTTP->HTTPS redirects are disabled","server_name":"srv0"}
[caddy] {"level":"warn","ts":1761356130.6543732,"logger":"pki.ca.local","msg":"installing root certificate (you might be prompted for password)","path":"storage:pki/authorities/local/root.crt"}
[caddy] {"level":"info","ts":1761356130.654513,"msg":"warning: \"certutil\" is not available, install \"certutil\" with \"apt install libnss3-tools\" or \"yum install nss-tools\" and try again"}
[caddy] {"level":"info","ts":1761356130.6545196,"msg":"define JAVA_HOME environment variable to use the Java trust"}
[caddy] {"level":"info","ts":1761356130.6686535,"msg":"certificate installed properly in linux trusts"}
[caddy] {"level":"info","ts":1761356130.6687553,"logger":"http","msg":"enabling HTTP/3 listener","addr":":443"}
[caddy] {"level":"info","ts":1761356130.6687775,"logger":"http.log","msg":"server running","name":"srv0","protocols":["h1","h2","h3"]}
[caddy] {"level":"info","ts":1761356130.6687837,"logger":"http","msg":"enabling automatic TLS certificate management","domains":["echo-mr.home.arpa","green.home.arpa","blue.home.arpa","docs.home.arpa","api.home.arpa"]}
[caddy] {"level":"info","ts":1761356130.6688015,"logger":"http","msg":"servers shutting down with eternal grace period"}
[caddy] {"level":"info","ts":1761356130.6689363,"msg":"autosaved config (load with --resume flag)","file":"/config/caddy/autosave.json"}
[caddy] {"level":"info","ts":1761356130.668947,"logger":"admin.api","msg":"load complete"}
[caddy] {"level":"info","ts":1761356130.67102,"logger":"admin","msg":"stopped previous server","address":"localhost:2019"}
[caddy] {"level":"info","ts":1761356130.9054098,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"33894","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356130.9055758,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356130.9055803,"logger":"admin.api","msg":"load complete"}
[caddy] 2025/10/25 01:35:30.936 INFO    http.log.access.log0    handled request{"request": {"remote_ip": "172.26.0.1", "remote_port": "37760", "client_ip": "172.26.0.1", "proto": "HTTP/2.0", "method": "GET", "host": "api.home.arpa:8443", "uri": "/openapi.json", "headers": {"User-Agent": ["curl/8.15.0"], "Accept": ["*/*"]}, "tls": {"resumed": false, "version": 772, "cipher_suite": 4865, "proto": "h2", "server_name": "api.home.arpa"}}, "bytes_read": 0, "user_id": "", "duration": 0.001745061, "size": 3393, "status": 200, "resp_headers": {"Server": ["Caddy", "BaseHTTP/0.6 Python/3.13.7"], "Alt-Svc": ["h3=\":443\"; ma=2592000"], "Date": ["Sat, 25 Oct 2025 01:35:30 GMT"], "Content-Type": ["application/json"], "Content-Length": ["3393"]}}
[caddy] {"level":"info","ts":1761356131.3174233,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"33896","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356131.3175888,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356131.3175945,"logger":"admin.api","msg":"load complete"}
[caddy] {"level":"info","ts":1761356131.847253,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"33908","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356131.847411,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356131.8474166,"logger":"admin.api","msg":"load complete"}
[controller] 2025-10-24 18:35:42 INFO __main__: http api listening on port 33583
[controller] 2025-10-24 18:35:42 WARNING __main__: watchdog not available; falling back to interval polling
[sites]
[sites] ==> state/caddy/blue.caddy <==
[sites] https://blue.home.arpa {
[sites]     log {
[sites]         output stdout
[sites]         format console
[sites]     }
[sites]     # Ensure upstream HSTS does not stick during dev
[sites]     header -Strict-Transport-Security
[sites]     reverse_proxy host.docker.internal:33176 {
[sites]
[sites]         lb_policy first
[sites]     }
[sites] }
[caddy] {"level":"info","ts":1761356142.84619,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"37380","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356142.8463676,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356142.8463764,"logger":"admin.api","msg":"load complete"}
[sites]
[sites] ==> state/caddy/green.caddy <==
[sites] https://green.home.arpa {
[sites]     log {
[sites]         output stdout
[sites]         format console
[sites]     }
[sites]     # Ensure upstream HSTS does not stick during dev
[sites]     header -Strict-Transport-Security
[sites]     reverse_proxy host.docker.internal:33178 {
[sites]
[sites]         lb_policy first
[sites]     }
[sites] }
[caddy] {"level":"info","ts":1761356142.9101918,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"37382","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356142.9103422,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356142.9103472,"logger":"admin.api","msg":"load complete"}
[caddy] {"level":"info","ts":1761356142.997574,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"37390","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356142.9977295,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356142.9977345,"logger":"admin.api","msg":"load complete"}
[caddy] {"level":"info","ts":1761356143.3742874,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"37402","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356143.3744373,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356143.3744423,"logger":"admin.api","msg":"load complete"}
[caddy] {"level":"info","ts":1761356143.5766935,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"37412","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356143.5767987,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356143.5768013,"logger":"admin.api","msg":"load complete"}
[caddy] {"level":"info","ts":1761356143.8474832,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"37420","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356143.8478756,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356143.847893,"logger":"admin.api","msg":"load complete"}
[caddy] {"level":"info","ts":1761356144.3373845,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"37424","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356144.337563,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356144.3375666,"logger":"admin.api","msg":"load complete"}
[caddy] {"level":"info","ts":1761356144.3428504,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"37438","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356144.3429966,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356144.3430018,"logger":"admin.api","msg":"load complete"}
[caddy] {"level":"info","ts":1761356144.9104438,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"37440","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356144.9105892,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356144.9105937,"logger":"admin.api","msg":"load complete"}
[caddy] {"level":"info","ts":1761356145.138653,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"37444","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356145.1388202,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356145.1388254,"logger":"admin.api","msg":"load complete"}
[caddy] {"level":"info","ts":1761356145.3691256,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"37454","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356145.369281,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356145.369288,"logger":"admin.api","msg":"load complete"}
[caddy] {"level":"info","ts":1761356145.8948267,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"37470","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356145.8949802,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356145.8949847,"logger":"admin.api","msg":"load complete"}
[controller] 2025-10-24 18:36:06 WARNING ae.runtime.docker_runtime: Failed to stop container ae-echo-rev205-0: 404 Client Error for http+docker://localhost/v1.45/containers/2626da3f68c5775ae8a51962343c045ec0926a1bb9dbb880c70b762f78e4a14a/stop?t=10: Not Found ("No such container: 2626da3f68c5775ae8a51962343c045ec0926a1bb9dbb880c70b762f78e4a14a")
[controller] 2025-10-24 18:36:06 WARNING ae.runtime.docker_runtime: Failed to remove container ae-echo-rev205-0: 404 Client Error for http+docker://localhost/v1.45/containers/2626da3f68c5775ae8a51962343c045ec0926a1bb9dbb880c70b762f78e4a14a?v=False&link=False&force=False: Not Found ("No such container: 2626da3f68c5775ae8a51962343c045ec0926a1bb9dbb880c70b762f78e4a14a")
[controller] 2025-10-24 18:36:06 WARNING ae.runtime.docker_runtime: Failed to stop container ae-echo-rev204-0: 404 Client Error for http+docker://localhost/v1.45/containers/3dfa6abe4e97e00b2beee7f13f111eec9971fda9675b858432781fac0f0cd7fc/stop?t=10: Not Found ("No such container: 3dfa6abe4e97e00b2beee7f13f111eec9971fda9675b858432781fac0f0cd7fc")
[controller] 2025-10-24 18:36:06 WARNING ae.runtime.docker_runtime: Failed to remove container ae-echo-rev204-0: 404 Client Error for http+docker://localhost/v1.45/containers/3dfa6abe4e97e00b2beee7f13f111eec9971fda9675b858432781fac0f0cd7fc?v=False&link=False&force=False: Not Found ("No such container: 3dfa6abe4e97e00b2beee7f13f111eec9971fda9675b858432781fac0f0cd7fc")
[controller] 2025-10-24 18:36:15 WARNING ae.runtime.docker_runtime: Failed to remove container ae-echo-rev203-0: 409 Client Error for http+docker://localhost/v1.45/containers/860f4adf66ee42dc2e144151f371cb46cead6936d8eb035e92c40f9471df7233?v=False&link=False&force=False: Conflict ("removal of container 860f4adf66ee42dc2e144151f371cb46cead6936d8eb035e92c40f9471df7233 is already in progress")
[controller] Traceback (most recent call last):
[controller]   File "/home/m4xx3d0ut/git/k1s/src/ae/secrets/manager.py", line 50, in _decrypt
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
[controller]   File "/home/m4xx3d0ut/git/k1s/src/ae/secrets/manager.py", line 65, in _decrypt
[controller]     raise RuntimeError(f"sops decrypt failed for {path}: {exc.stderr}") from exc
[controller] RuntimeError: sops decrypt failed for specs/examples/demo-secret.sops.yaml: sops metadata not found
[controller]
[controller] Applied blue: +0/~0/-0, ready=1, live=1, rev=3(ready)
[controller] Applied echo: +1/~0/-0, ready=0, live=1, rev=205(progressing)
[controller] Applied echo-stateful: +0/~0/-0, ready=1, live=1, rev=1(ready)
[controller] Applied echo-del: +0/~0/-0, ready=1, live=1, rev=1(ready)
[controller] Applied echo: +1/~0/-0, ready=1, live=1, rev=206(ready)
[caddy] {"level":"info","ts":1761356176.4275932,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"51236","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356176.4277494,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356176.4277542,"logger":"admin.api","msg":"load complete"}
[sites] https://green.home.arpa {
[sites]     log {
[sites]         output stdout
[sites]         format console
[sites]     }
[sites]     # Ensure upstream HSTS does not stick during dev
[sites]     header -Strict-Transport-Security
[sites]     reverse_proxy host.docker.internal:33178 {
[sites]
[sites]         lb_policy first
[sites]     }
[sites] }
[caddy] {"level":"info","ts":1761356176.634825,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"51246","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356176.6350455,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356176.6350517,"logger":"admin.api","msg":"load complete"}
[sites]
[sites] ==> state/caddy/echo-mr.caddy <==
[sites] https://echo-mr.home.arpa {
[sites]     log {
[sites]         output stdout
[sites]         format console
[sites]     }
[sites]     # Ensure upstream HSTS does not stick during dev
[sites]     header -Strict-Transport-Security
[sites]     reverse_proxy host.docker.internal:33187 host.docker.internal:33186 host.docker.internal:33185 {
[sites]
[sites]         lb_policy first
[sites]     }
[sites] }
[caddy] {"level":"info","ts":1761356176.873993,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"51256","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356176.874698,"logger":"admin","msg":"admin endpoint started","address":"localhost:2019","enforce_origin":false,"origins":["//127.0.0.1:2019","//localhost:2019","//[::1]:2019"]}
[caddy] {"level":"info","ts":1761356176.8748717,"logger":"http.auto_https","msg":"server is listening only on the HTTPS port but has no TLS connection policies; adding one to enable TLS","server_name":"srv0","https_port":443}
[caddy] {"level":"info","ts":1761356176.8748903,"logger":"http.auto_https","msg":"automatic HTTP->HTTPS redirects are disabled","server_name":"srv0"}
[caddy] {"level":"warn","ts":1761356176.875465,"logger":"pki.ca.local","msg":"installing root certificate (you might be prompted for password)","path":"storage:pki/authorities/local/root.crt"}
[caddy] {"level":"info","ts":1761356176.8755834,"msg":"warning: \"certutil\" is not available, install \"certutil\" with \"apt install libnss3-tools\" or \"yum install nss-tools\" and try again"}
[caddy] {"level":"info","ts":1761356176.8755877,"msg":"define JAVA_HOME environment variable to use the Java trust"}
[caddy] {"level":"info","ts":1761356176.88898,"msg":"certificate installed properly in linux trusts"}
[caddy] {"level":"info","ts":1761356176.8891373,"logger":"http","msg":"enabling HTTP/3 listener","addr":":443"}
[caddy] {"level":"info","ts":1761356176.8891573,"logger":"http.log","msg":"server running","name":"srv0","protocols":["h1","h2","h3"]}
[caddy] {"level":"info","ts":1761356176.889161,"logger":"http","msg":"enabling automatic TLS certificate management","domains":["blue.home.arpa","docs.home.arpa","api.home.arpa","echo-mr.home.arpa","green.home.arpa"]}
[caddy] {"level":"info","ts":1761356176.8891761,"logger":"http","msg":"servers shutting down with eternal grace period"}
[caddy] {"level":"info","ts":1761356176.8893647,"msg":"autosaved config (load with --resume flag)","file":"/config/caddy/autosave.json"}
[caddy] {"level":"info","ts":1761356176.8893754,"logger":"admin.api","msg":"load complete"}
[caddy] {"level":"info","ts":1761356176.8923721,"logger":"admin","msg":"stopped previous server","address":"localhost:2019"}
[sites]
[sites] ==> state/caddy/blue.caddy <==
[sites] https://blue.home.arpa {
[sites]     log {
[sites]         output stdout
[sites]         format console
[sites]     }
[sites]     # Ensure upstream HSTS does not stick during dev
[sites]     header -Strict-Transport-Security
[sites]     reverse_proxy host.docker.internal:33176 {
[sites]
[sites]         lb_policy first
[sites]     }
[sites] }
[caddy] {"level":"info","ts":1761356177.331483,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"51266","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356177.3315973,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356177.3316002,"logger":"admin.api","msg":"load complete"}
[caddy] {"level":"info","ts":1761356177.8651414,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"51272","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356177.865289,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356177.8652956,"logger":"admin.api","msg":"load complete"}
[caddy] {"level":"info","ts":1761356178.0864928,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"51284","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356178.0866466,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356178.0866516,"logger":"admin.api","msg":"load complete"}
[caddy] {"level":"info","ts":1761356178.3175633,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"51300","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356178.3177242,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356178.3177292,"logger":"admin.api","msg":"load complete"}
[caddy] {"level":"info","ts":1761356178.7996354,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"51302","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356178.7997842,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356178.799789,"logger":"admin.api","msg":"load complete"}
^[[A^[[B[controller] 2025-10-24 18:36:48 INFO __main__: http api listening on port 37033
[controller] 2025-10-24 18:36:48 WARNING __main__: watchdog not available; falling back to interval polling
[sites] https://blue.home.arpa {
[sites]     log {
[sites]         output stdout
[sites]         format console
[sites]     }
[sites]     # Ensure upstream HSTS does not stick during dev
[sites]     header -Strict-Transport-Security
[sites]     reverse_proxy host.docker.internal:33176 {
[sites]
[sites]         lb_policy first
[sites]     }
[sites] }
[caddy] {"level":"info","ts":1761356208.6304948,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"50540","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356208.6306481,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356208.6306524,"logger":"admin.api","msg":"load complete"}
[caddy] {"level":"info","ts":1761356209.1466372,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"50542","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356209.1468,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356209.1468062,"logger":"admin.api","msg":"load complete"}
[caddy] {"level":"info","ts":1761356209.3997736,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"50558","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356209.3999412,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356209.399948,"logger":"admin.api","msg":"load complete"}
[caddy] {"level":"info","ts":1761356209.6229339,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"50564","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356209.6231356,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356209.623143,"logger":"admin.api","msg":"load complete"}
[caddy] {"level":"info","ts":1761356210.4676914,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"52520","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356210.4678893,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356210.4678948,"logger":"admin.api","msg":"load complete"}
[caddy] {"level":"info","ts":1761356210.8424165,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"52536","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356210.842564,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356210.8425698,"logger":"admin.api","msg":"load complete"}
[sites]
[sites] ==> state/caddy/green.caddy <==
[sites] https://green.home.arpa {
[sites]     log {
[sites]         output stdout
[sites]         format console
[sites]     }
[sites]     # Ensure upstream HSTS does not stick during dev
[sites]     header -Strict-Transport-Security
[sites]     reverse_proxy host.docker.internal:33178 {
[sites]
[sites]         lb_policy first
[sites]     }
[sites] }
[caddy] {"level":"info","ts":1761356211.0249074,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"52542","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356211.025003,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356211.0250056,"logger":"admin.api","msg":"load complete"}
[sites]
[sites] ==> state/caddy/echo-mr.caddy <==
[sites] https://echo-mr.home.arpa {
[sites]     log {
[sites]         output stdout
[sites]         format console
[sites]     }
[sites]     # Ensure upstream HSTS does not stick during dev
[sites]     header -Strict-Transport-Security
[sites]     reverse_proxy host.docker.internal:33187 host.docker.internal:33186 host.docker.internal:33185 {
[sites]
[sites]         lb_policy first
[sites]     }
[sites] }
[caddy] {"level":"info","ts":1761356211.2583888,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"52554","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356211.258534,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356211.258539,"logger":"admin.api","msg":"load complete"}
[sites]
[sites] ==> state/caddy/blue.caddy <==
[sites] https://blue.home.arpa {
[sites]     log {
[sites]         output stdout
[sites]         format console
[sites]     }
[sites]     # Ensure upstream HSTS does not stick during dev
[sites]     header -Strict-Transport-Security
[sites]     reverse_proxy host.docker.internal:33176 {
[sites]
[sites]         lb_policy first
[sites]     }
[sites] }
[caddy] {"level":"info","ts":1761356211.7301826,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"52570","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356211.7303326,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356211.7303412,"logger":"admin.api","msg":"load complete"}
[caddy] {"level":"info","ts":1761356212.30204,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"52576","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356212.302191,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356212.3021958,"logger":"admin.api","msg":"load complete"}
[caddy] {"level":"info","ts":1761356212.5409422,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"52588","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356212.541045,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356212.5410473,"logger":"admin.api","msg":"load complete"}
[caddy] {"level":"info","ts":1761356212.7660782,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"52594","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356212.7662358,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356212.7662416,"logger":"admin.api","msg":"load complete"}
[caddy] {"level":"info","ts":1761356213.3529658,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"52608","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356213.3531363,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356213.353142,"logger":"admin.api","msg":"load complete"}
[controller] 2025-10-24 18:37:03 WARNING ae.runtime.docker_runtime: Failed to stop container ae-echo-rev214-0: 404 Client Error for http+docker://localhost/v1.45/containers/75a3c8fe679373a4a7197e3a5cb9b0396d3873f10b91d9fc2c7e9b2c24336987/stop?t=10: Not Found ("No such container: 75a3c8fe679373a4a7197e3a5cb9b0396d3873f10b91d9fc2c7e9b2c24336987")
[controller] 2025-10-24 18:37:03 WARNING ae.runtime.docker_runtime: Failed to remove container ae-echo-rev214-0: 404 Client Error for http+docker://localhost/v1.45/containers/75a3c8fe679373a4a7197e3a5cb9b0396d3873f10b91d9fc2c7e9b2c24336987?v=False&link=False&force=False: Not Found ("No such container: 75a3c8fe679373a4a7197e3a5cb9b0396d3873f10b91d9fc2c7e9b2c24336987")
[controller] 2025-10-24 18:37:14 WARNING ae.runtime.docker_runtime: Failed to stop container ae-echo-rev212-0: 404 Client Error for http+docker://localhost/v1.45/containers/a6b3890bae8e8424e82da5ce08739b4ddab1a506d74bf6524998f72b159a6d4b/stop?t=10: Not Found ("No such container: a6b3890bae8e8424e82da5ce08739b4ddab1a506d74bf6524998f72b159a6d4b")
[controller] 2025-10-24 18:37:14 WARNING ae.runtime.docker_runtime: Failed to remove container ae-echo-rev212-0: 404 Client Error for http+docker://localhost/v1.45/containers/a6b3890bae8e8424e82da5ce08739b4ddab1a506d74bf6524998f72b159a6d4b?v=False&link=False&force=False: Not Found ("No such container: a6b3890bae8e8424e82da5ce08739b4ddab1a506d74bf6524998f72b159a6d4b")
[controller] 2025-10-24 18:37:22 WARNING ae.runtime.docker_runtime: Failed to remove container ae-echo-rev211-0: 409 Client Error for http+docker://localhost/v1.45/containers/ed1929b82f036d0f903e7481a42462a721a5a5e2832f994124cc96f3ca31c90f?v=False&link=False&force=False: Conflict ("removal of container ed1929b82f036d0f903e7481a42462a721a5a5e2832f994124cc96f3ca31c90f is already in progress")
[controller] Traceback (most recent call last):
[controller]   File "/home/m4xx3d0ut/git/k1s/src/ae/secrets/manager.py", line 50, in _decrypt
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
[controller]   File "/home/m4xx3d0ut/git/k1s/src/ae/secrets/manager.py", line 65, in _decrypt
[controller]     raise RuntimeError(f"sops decrypt failed for {path}: {exc.stderr}") from exc
[controller] RuntimeError: sops decrypt failed for specs/examples/demo-secret.sops.yaml: sops metadata not found
[controller]
[controller] Applied blue: +0/~0/-0, ready=1, live=1, rev=3(ready)
[controller] Applied echo: +1/~0/-0, ready=0, live=1, rev=212(progressing)
[controller] Applied echo-stateful: +0/~0/-0, ready=1, live=1, rev=1(ready)
[controller] Applied echo-del: +0/~0/-0, ready=1, live=1, rev=1(ready)
[controller] Applied echo: +1/~0/-0, ready=1, live=1, rev=213(ready)
[caddy] {"level":"info","ts":1761356242.5886707,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"56800","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356242.5888348,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356242.588841,"logger":"admin.api","msg":"load complete"}
[sites]
[sites] ==> state/caddy/green.caddy <==
[sites] https://green.home.arpa {
[sites]     log {
[sites]         output stdout
[sites]         format console
[sites]     }
[sites]     # Ensure upstream HSTS does not stick during dev
[sites]     header -Strict-Transport-Security
[sites]     reverse_proxy host.docker.internal:33178 {
[sites]
[sites]         lb_policy first
[sites]     }
[sites] }
[caddy] {"level":"info","ts":1761356242.8567672,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"56810","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356242.8569224,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356242.8569276,"logger":"admin.api","msg":"load complete"}
[sites]
[sites] ==> state/caddy/echo-mr.caddy <==
[sites] https://echo-mr.home.arpa {
[sites]     log {
[sites]         output stdout
[sites]         format console
[sites]     }
[sites]     # Ensure upstream HSTS does not stick during dev
[sites]     header -Strict-Transport-Security
[sites]     reverse_proxy host.docker.internal:33187 host.docker.internal:33186 host.docker.internal:33185 {
[sites]
[sites]         lb_policy first
[sites]     }
[sites] }
[caddy] {"level":"info","ts":1761356243.094649,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"56814","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356243.094796,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356243.094802,"logger":"admin.api","msg":"load complete"}
[sites]
[sites] ==> state/caddy/blue.caddy <==
[sites] https://blue.home.arpa {
[sites]     log {
[sites]         output stdout
[sites]         format console
[sites]     }
[sites]     # Ensure upstream HSTS does not stick during dev
[sites]     header -Strict-Transport-Security
[sites]     reverse_proxy host.docker.internal:33176 {
[sites]
[sites]         lb_policy first
[sites]     }
[sites] }
[caddy] {"level":"info","ts":1761356243.528698,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"56822","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356243.5288496,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356243.5288548,"logger":"admin.api","msg":"load complete"}
[caddy] {"level":"info","ts":1761356244.070305,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"56834","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356244.0704563,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356244.0704613,"logger":"admin.api","msg":"load complete"}
[caddy] {"level":"info","ts":1761356244.293955,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"56844","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356244.2941117,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356244.2941198,"logger":"admin.api","msg":"load complete"}
[caddy] {"level":"info","ts":1761356244.5135741,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"56854","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356244.5137296,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356244.513736,"logger":"admin.api","msg":"load complete"}
[caddy] {"level":"info","ts":1761356245.0566313,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"56856","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356245.056775,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356245.0567787,"logger":"admin.api","msg":"load complete"}
[controller] 2025-10-24 18:37:54 INFO __main__: http api listening on port 38839
[controller] 2025-10-24 18:37:54 WARNING __main__: watchdog not available; falling back to interval polling
[sites] https://blue.home.arpa {
[sites]     log {
[sites]         output stdout
[sites]         format console
[sites]     }
[sites]     # Ensure upstream HSTS does not stick during dev
[sites]     header -Strict-Transport-Security
[sites]     reverse_proxy host.docker.internal:33176 {
[sites]
[sites]         lb_policy first
[sites]     }
[sites] }
[caddy] {"level":"info","ts":1761356274.8599582,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"39132","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356274.8601053,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356274.8601098,"logger":"admin.api","msg":"load complete"}
[caddy] {"level":"info","ts":1761356275.3760338,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"39134","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356275.3761806,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356275.376186,"logger":"admin.api","msg":"load complete"}
[caddy] {"level":"info","ts":1761356275.588276,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"39142","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356275.5884185,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356275.5884235,"logger":"admin.api","msg":"load complete"}
[caddy] {"level":"info","ts":1761356275.7944071,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"39146","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356275.7945726,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356275.794578,"logger":"admin.api","msg":"load complete"}
[caddy] {"level":"info","ts":1761356276.6487906,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"39156","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356276.6489341,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356276.6489387,"logger":"admin.api","msg":"load complete"}
[caddy] {"level":"info","ts":1761356276.9646516,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"39166","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356276.9648058,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356276.9648106,"logger":"admin.api","msg":"load complete"}
[sites]
[sites] ==> state/caddy/green.caddy <==
[sites] https://green.home.arpa {
[sites]     log {
[sites]         output stdout
[sites]         format console
[sites]     }
[sites]     # Ensure upstream HSTS does not stick during dev
[sites]     header -Strict-Transport-Security
[sites]     reverse_proxy host.docker.internal:33178 {
[sites]
[sites]         lb_policy first
[sites]     }
[sites] }
[caddy] {"level":"info","ts":1761356277.194097,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"39168","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356277.1942518,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356277.1942568,"logger":"admin.api","msg":"load complete"}
[sites]
[sites] ==> state/caddy/echo-mr.caddy <==
[sites] https://echo-mr.home.arpa {
[sites]     log {
[sites]         output stdout
[sites]         format console
[sites]     }
[sites]     # Ensure upstream HSTS does not stick during dev
[sites]     header -Strict-Transport-Security
[sites]     reverse_proxy host.docker.internal:33187 host.docker.internal:33186 host.docker.internal:33185 {
[sites]
[sites]         lb_policy first
[sites]     }
[sites] }
[caddy] {"level":"info","ts":1761356277.440378,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"39180","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356277.4405475,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356277.4405735,"logger":"admin.api","msg":"load complete"}
[caddy] {"level":"info","ts":1761356277.8852952,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"39186","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356277.885393,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356277.8853955,"logger":"admin.api","msg":"load complete"}
[caddy] {"level":"info","ts":1761356278.3793254,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"39200","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356278.379498,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356278.379504,"logger":"admin.api","msg":"load complete"}
[caddy] {"level":"info","ts":1761356278.610809,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"39216","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356278.610958,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356278.610964,"logger":"admin.api","msg":"load complete"}
[caddy] {"level":"info","ts":1761356278.831785,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"39222","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356278.8319483,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356278.831956,"logger":"admin.api","msg":"load complete"}
[caddy] {"level":"info","ts":1761356279.3608687,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"39230","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356279.361025,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356279.3610303,"logger":"admin.api","msg":"load complete"}
[controller] 2025-10-24 18:38:09 WARNING ae.runtime.docker_runtime: Failed to stop container ae-echo-rev222-0: 404 Client Error for http+docker://localhost/v1.45/containers/760d043820f4a726f7e9150a3c8fe09d2ca434fa654a9ec4eccba297d51aa2f3/stop?t=10: Not Found ("No such container: 760d043820f4a726f7e9150a3c8fe09d2ca434fa654a9ec4eccba297d51aa2f3")
[controller] 2025-10-24 18:38:09 WARNING ae.runtime.docker_runtime: Failed to remove container ae-echo-rev222-0: 404 Client Error for http+docker://localhost/v1.45/containers/760d043820f4a726f7e9150a3c8fe09d2ca434fa654a9ec4eccba297d51aa2f3?v=False&link=False&force=False: Not Found ("No such container: 760d043820f4a726f7e9150a3c8fe09d2ca434fa654a9ec4eccba297d51aa2f3")
[controller] 2025-10-24 18:38:20 WARNING ae.runtime.docker_runtime: Failed to stop container ae-echo-rev220-0: 404 Client Error for http+docker://localhost/v1.45/containers/c8a9d1b13c214cf6e545f29d571a81dc86a13807a3535f9566d31047716559d7/stop?t=10: Not Found ("No such container: c8a9d1b13c214cf6e545f29d571a81dc86a13807a3535f9566d31047716559d7")
[controller] 2025-10-24 18:38:20 WARNING ae.runtime.docker_runtime: Failed to remove container ae-echo-rev220-0: 404 Client Error for http+docker://localhost/v1.45/containers/c8a9d1b13c214cf6e545f29d571a81dc86a13807a3535f9566d31047716559d7?v=False&link=False&force=False: Not Found ("No such container: c8a9d1b13c214cf6e545f29d571a81dc86a13807a3535f9566d31047716559d7")
[controller] 2025-10-24 18:38:28 WARNING ae.runtime.docker_runtime: Failed to remove container ae-echo-rev219-0: 409 Client Error for http+docker://localhost/v1.45/containers/931805c62842b587695ce48f05039fd315bd15c92bfe1d9e7736ef9588c4390d?v=False&link=False&force=False: Conflict ("removal of container 931805c62842b587695ce48f05039fd315bd15c92bfe1d9e7736ef9588c4390d is already in progress")
[controller] Traceback (most recent call last):
[controller]   File "/home/m4xx3d0ut/git/k1s/src/ae/secrets/manager.py", line 50, in _decrypt
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
[controller]   File "/home/m4xx3d0ut/git/k1s/src/ae/secrets/manager.py", line 65, in _decrypt
[controller]     raise RuntimeError(f"sops decrypt failed for {path}: {exc.stderr}") from exc
[controller] RuntimeError: sops decrypt failed for specs/examples/demo-secret.sops.yaml: sops metadata not found
[controller]
[controller] Applied blue: +0/~0/-0, ready=1, live=1, rev=3(ready)
[controller] Applied echo: +1/~0/-0, ready=0, live=1, rev=220(progressing)
[controller] Applied echo-stateful: +0/~0/-0, ready=1, live=1, rev=1(ready)
[controller] Applied echo-del: +0/~0/-0, ready=1, live=1, rev=1(ready)
[controller] Applied echo: +1/~0/-0, ready=1, live=1, rev=221(ready)
[caddy] {"level":"info","ts":1761356308.7502596,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"36430","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356308.7504163,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356308.7504213,"logger":"admin.api","msg":"load complete"}
[sites]
[sites] ==> state/caddy/green.caddy <==
[sites] https://green.home.arpa {
[sites]     log {
[sites]         output stdout
[sites]         format console
[sites]     }
[sites]     # Ensure upstream HSTS does not stick during dev
[sites]     header -Strict-Transport-Security
[sites]     reverse_proxy host.docker.internal:33178 {
[sites]
[sites]         lb_policy first
[sites]     }
[sites] }
[caddy] {"level":"info","ts":1761356308.969034,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"36438","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356308.9691315,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356308.9691343,"logger":"admin.api","msg":"load complete"}
[sites]
[sites] ==> state/caddy/echo-mr.caddy <==
[sites] https://echo-mr.home.arpa {
[sites]     log {
[sites]         output stdout
[sites]         format console
[sites]     }
[sites]     # Ensure upstream HSTS does not stick during dev
[sites]     header -Strict-Transport-Security
[sites]     reverse_proxy host.docker.internal:33187 host.docker.internal:33186 host.docker.internal:33185 {
[sites]
[sites]         lb_policy first
[sites]     }
[sites] }
[caddy] {"level":"info","ts":1761356309.1873002,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"36446","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356309.187405,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356309.187408,"logger":"admin.api","msg":"load complete"}
[sites]
[sites] ==> state/caddy/blue.caddy <==
[sites] https://blue.home.arpa {
[sites]     log {
[sites]         output stdout
[sites]         format console
[sites]     }
[sites]     # Ensure upstream HSTS does not stick during dev
[sites]     header -Strict-Transport-Security
[sites]     reverse_proxy host.docker.internal:33176 {
[sites]
[sites]         lb_policy first
[sites]     }
[sites] }
[caddy] {"level":"info","ts":1761356309.647575,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"36456","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356309.6477282,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356309.6477332,"logger":"admin.api","msg":"load complete"}
[caddy] {"level":"info","ts":1761356310.1543477,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"60392","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356310.1544967,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356310.1545017,"logger":"admin.api","msg":"load complete"}
[caddy] {"level":"info","ts":1761356310.352276,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"60406","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356310.3524399,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356310.352445,"logger":"admin.api","msg":"load complete"}
[caddy] {"level":"info","ts":1761356310.5569096,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"60408","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356310.5570688,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356310.557073,"logger":"admin.api","msg":"load complete"}
[caddy] {"level":"info","ts":1761356311.1270983,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"60418","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356311.127286,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356311.1272955,"logger":"admin.api","msg":"load complete"}
[controller] 2025-10-24 18:39:00 INFO __main__: http api listening on port 33147
[controller] 2025-10-24 18:39:00 WARNING __main__: watchdog not available; falling back to interval polling
[sites] https://blue.home.arpa {
[sites]     log {
[sites]         output stdout
[sites]         format console
[sites]     }
[sites]     # Ensure upstream HSTS does not stick during dev
[sites]     header -Strict-Transport-Security
[sites]     reverse_proxy host.docker.internal:33176 {
[sites]
[sites]         lb_policy first
[sites]     }
[sites] }
[caddy] {"level":"info","ts":1761356341.078029,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"39760","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356341.0781932,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356341.0781982,"logger":"admin.api","msg":"load complete"}
[caddy] {"level":"info","ts":1761356341.5467618,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"39776","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356341.5469162,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356341.5469215,"logger":"admin.api","msg":"load complete"}
[caddy] {"level":"info","ts":1761356341.6914065,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"39782","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356341.691506,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356341.6915088,"logger":"admin.api","msg":"load complete"}
[caddy] {"level":"info","ts":1761356341.9069388,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"39784","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356341.9070919,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356341.9070966,"logger":"admin.api","msg":"load complete"}
[caddy] {"level":"info","ts":1761356342.8089697,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"39786","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356342.8090794,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356342.8090818,"logger":"admin.api","msg":"load complete"}
[caddy] {"level":"info","ts":1761356343.133071,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"39798","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356343.1332166,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356343.133221,"logger":"admin.api","msg":"load complete"}
[sites]
[sites] ==> state/caddy/green.caddy <==
[sites] https://green.home.arpa {
[sites]     log {
[sites]         output stdout
[sites]         format console
[sites]     }
[sites]     # Ensure upstream HSTS does not stick during dev
[sites]     header -Strict-Transport-Security
[sites]     reverse_proxy host.docker.internal:33178 {
[sites]
[sites]         lb_policy first
[sites]     }
[sites] }
[caddy] {"level":"info","ts":1761356343.3785043,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"39802","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356343.3786538,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356343.3786588,"logger":"admin.api","msg":"load complete"}
[sites]
[sites] ==> state/caddy/echo-mr.caddy <==
[sites] https://echo-mr.home.arpa {
[sites]     log {
[sites]         output stdout
[sites]         format console
[sites]     }
[sites]     # Ensure upstream HSTS does not stick during dev
[sites]     header -Strict-Transport-Security
[sites]     reverse_proxy host.docker.internal:33187 host.docker.internal:33186 host.docker.internal:33185 {
[sites]
[sites]         lb_policy first
[sites]     }
[sites] }
[caddy] {"level":"info","ts":1761356343.600354,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"39818","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356343.6005008,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356343.6005058,"logger":"admin.api","msg":"load complete"}
[sites]
[sites] ==> state/caddy/blue.caddy <==
[sites] https://blue.home.arpa {
[sites]     log {
[sites]         output stdout
[sites]         format console
[sites]     }
[sites]     # Ensure upstream HSTS does not stick during dev
[sites]     header -Strict-Transport-Security
[sites]     reverse_proxy host.docker.internal:33176 {
[sites]
[sites]         lb_policy first
[sites]     }
[sites] }
[caddy] {"level":"info","ts":1761356344.0653894,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"39834","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356344.0655391,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356344.0655434,"logger":"admin.api","msg":"load complete"}
[caddy] {"level":"info","ts":1761356344.5837348,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"39848","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356344.583881,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356344.5838864,"logger":"admin.api","msg":"load complete"}
[caddy] {"level":"info","ts":1761356344.8042934,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"39858","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356344.8044457,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356344.8044505,"logger":"admin.api","msg":"load complete"}
[caddy] {"level":"info","ts":1761356345.0207467,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"39874","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356345.0208924,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356345.0208967,"logger":"admin.api","msg":"load complete"}
[caddy] {"level":"info","ts":1761356345.5147882,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"39888","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356345.5149343,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356345.5149384,"logger":"admin.api","msg":"load complete"}
[controller] 2025-10-24 18:39:15 WARNING ae.runtime.docker_runtime: Failed to stop container ae-echo-rev230-0: 404 Client Error for http+docker://localhost/v1.45/containers/8bdf42b4844c6f386e15a2719b5272c44cde1315440e9492439152fb22cecec4/stop?t=10: Not Found ("No such container: 8bdf42b4844c6f386e15a2719b5272c44cde1315440e9492439152fb22cecec4")
[controller] 2025-10-24 18:39:15 WARNING ae.runtime.docker_runtime: Failed to remove container ae-echo-rev230-0: 404 Client Error for http+docker://localhost/v1.45/containers/8bdf42b4844c6f386e15a2719b5272c44cde1315440e9492439152fb22cecec4?v=False&link=False&force=False: Not Found ("No such container: 8bdf42b4844c6f386e15a2719b5272c44cde1315440e9492439152fb22cecec4")
[controller] 2025-10-24 18:39:26 WARNING ae.runtime.docker_runtime: Failed to stop container ae-echo-rev228-0: 404 Client Error for http+docker://localhost/v1.45/containers/e64751ab17c5dd84a20ae82936ec36fb6d65ac941c1e8853173d9bd4fbb1846f/stop?t=10: Not Found ("No such container: e64751ab17c5dd84a20ae82936ec36fb6d65ac941c1e8853173d9bd4fbb1846f")
[controller] 2025-10-24 18:39:26 WARNING ae.runtime.docker_runtime: Failed to remove container ae-echo-rev228-0: 404 Client Error for http+docker://localhost/v1.45/containers/e64751ab17c5dd84a20ae82936ec36fb6d65ac941c1e8853173d9bd4fbb1846f?v=False&link=False&force=False: Not Found ("No such container: e64751ab17c5dd84a20ae82936ec36fb6d65ac941c1e8853173d9bd4fbb1846f")
[controller] 2025-10-24 18:39:34 WARNING ae.runtime.docker_runtime: Failed to remove container ae-echo-rev227-0: 409 Client Error for http+docker://localhost/v1.45/containers/bb0494d8a94129d625666b3e0cdc12376a994928e1a23c68e98086dd0818c9c0?v=False&link=False&force=False: Conflict ("removal of container bb0494d8a94129d625666b3e0cdc12376a994928e1a23c68e98086dd0818c9c0 is already in progress")
[caddy] {"level":"info","ts":1761356374.9338157,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"46052","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356374.9339592,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356374.9339645,"logger":"admin.api","msg":"load complete"}
[sites]
[sites] ==> state/caddy/green.caddy <==
[sites] https://green.home.arpa {
[sites]     log {
[sites]         output stdout
[sites]         format console
[sites]     }
[sites]     # Ensure upstream HSTS does not stick during dev
[sites]     header -Strict-Transport-Security
[sites]     reverse_proxy host.docker.internal:33178 {
[sites]
[sites]         lb_policy first
[sites]     }
[sites] }
[caddy] {"level":"info","ts":1761356375.1415606,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"46054","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356375.1417139,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356375.1417189,"logger":"admin.api","msg":"load complete"}
[sites]
[sites] ==> state/caddy/echo-mr.caddy <==
[sites] https://echo-mr.home.arpa {
[sites]     log {
[sites]         output stdout
[sites]         format console
[sites]     }
[sites]     # Ensure upstream HSTS does not stick during dev
[sites]     header -Strict-Transport-Security
[sites]     reverse_proxy host.docker.internal:33187 host.docker.internal:33186 host.docker.internal:33185 {
[sites]
[sites]         lb_policy first
[sites]     }
[sites] }
[caddy] {"level":"info","ts":1761356375.3684196,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"46064","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356375.3685832,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356375.3685896,"logger":"admin.api","msg":"load complete"}
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
[controller] Applied echo: +1/~0/-0, ready=0, live=1, rev=228(progressing)
[controller] Applied echo-stateful: +0/~0/-0, ready=1, live=1, rev=1(ready)
[controller] Applied echo-del: +0/~0/-0, ready=1, live=1, rev=1(ready)
[controller] Applied echo: +1/~0/-0, ready=1, live=1, rev=229(ready)
[caddy] {"level":"info","ts":1761356375.8215852,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"46072","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356375.8217404,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356375.8217452,"logger":"admin.api","msg":"load complete"}
[caddy] {"level":"info","ts":1761356376.347291,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"46078","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356376.3473911,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356376.3473938,"logger":"admin.api","msg":"load complete"}
[caddy] {"level":"info","ts":1761356376.5378754,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"46092","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356376.5380237,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356376.5380285,"logger":"admin.api","msg":"load complete"}
[caddy] {"level":"info","ts":1761356376.7732396,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"46106","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356376.773389,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356376.7733932,"logger":"admin.api","msg":"load complete"}
[caddy] {"level":"info","ts":1761356377.2902803,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"46120","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356377.2904484,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356377.2904527,"logger":"admin.api","msg":"load complete"}
[controller] 2025-10-24 18:40:07 INFO __main__: http api listening on port 38227
[controller] 2025-10-24 18:40:07 WARNING __main__: watchdog not available; falling back to interval polling
[sites]
[sites] ==> state/caddy/blue.caddy <==
[sites] https://blue.home.arpa {
[sites]     log {
[sites]         output stdout
[sites]         format console
[sites]     }
[sites]     # Ensure upstream HSTS does not stick during dev
[sites]     header -Strict-Transport-Security
[sites]     reverse_proxy host.docker.internal:33176 {
[sites]
[sites]         lb_policy first
[sites]     }
[sites] }
[caddy] {"level":"info","ts":1761356407.8027864,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"54784","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356407.8029382,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356407.8029435,"logger":"admin.api","msg":"load complete"}
[caddy] {"level":"info","ts":1761356408.3671272,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"54792","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356408.367276,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356408.3672805,"logger":"admin.api","msg":"load complete"}
[caddy] {"level":"info","ts":1761356408.570886,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"54802","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356408.5710368,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356408.5710418,"logger":"admin.api","msg":"load complete"}
[caddy] {"level":"info","ts":1761356408.7959588,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"54814","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356408.7961173,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356408.7961237,"logger":"admin.api","msg":"load complete"}
[caddy] {"level":"info","ts":1761356409.4243813,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"54828","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356409.4245524,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356409.4245582,"logger":"admin.api","msg":"load complete"}
[sites]
[sites] ==> state/caddy/green.caddy <==
[sites] https://green.home.arpa {
[sites]     log {
[sites]         output stdout
[sites]         format console
[sites]     }
[sites]     # Ensure upstream HSTS does not stick during dev
[sites]     header -Strict-Transport-Security
[sites]     reverse_proxy host.docker.internal:33178 {
[sites]
[sites]         lb_policy first
[sites]     }
[sites] }
[caddy] {"level":"info","ts":1761356409.4922538,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"54832","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356409.4924207,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356409.4924257,"logger":"admin.api","msg":"load complete"}
[caddy] {"level":"info","ts":1761356409.6433768,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"54842","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356409.6435385,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356409.643544,"logger":"admin.api","msg":"load complete"}
[sites]
[sites] ==> state/caddy/echo-mr.caddy <==
[sites] https://echo-mr.home.arpa {
[sites]     log {
[sites]         output stdout
[sites]         format console
[sites]     }
[sites]     # Ensure upstream HSTS does not stick during dev
[sites]     header -Strict-Transport-Security
[sites]     reverse_proxy host.docker.internal:33187 host.docker.internal:33186 host.docker.internal:33185 {
[sites]
[sites]         lb_policy first
[sites]     }
[sites] }
[caddy] {"level":"info","ts":1761356409.8848922,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"54850","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356409.8850398,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356409.8850446,"logger":"admin.api","msg":"load complete"}
[sites]
[sites] ==> state/caddy/blue.caddy <==
[sites] https://blue.home.arpa {
[sites]     log {
[sites]         output stdout
[sites]         format console
[sites]     }
[sites]     # Ensure upstream HSTS does not stick during dev
[sites]     header -Strict-Transport-Security
[sites]     reverse_proxy host.docker.internal:33176 {
[sites]
[sites]         lb_policy first
[sites]     }
[sites] }
[caddy] {"level":"info","ts":1761356410.321769,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"45992","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356410.3219287,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356410.3219335,"logger":"admin.api","msg":"load complete"}
[caddy] {"level":"info","ts":1761356410.87978,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"45998","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356410.8799613,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356410.879969,"logger":"admin.api","msg":"load complete"}
[caddy] {"level":"info","ts":1761356411.0998702,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"46002","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356411.100027,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356411.1000326,"logger":"admin.api","msg":"load complete"}
[caddy] {"level":"info","ts":1761356411.3264167,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"46004","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356411.3265786,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356411.326584,"logger":"admin.api","msg":"load complete"}
[caddy] {"level":"info","ts":1761356411.8817835,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"46008","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356411.8819318,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356411.8819366,"logger":"admin.api","msg":"load complete"}
[controller] 2025-10-24 18:40:32 WARNING ae.runtime.docker_runtime: Failed to stop container ae-echo-rev237-0: 404 Client Error for http+docker://localhost/v1.45/containers/5b16f25d1ca2583488e1290c8aec7e2da16c084e949584d3f64d73a17a7b29d6/stop?t=10: Not Found ("No such container: 5b16f25d1ca2583488e1290c8aec7e2da16c084e949584d3f64d73a17a7b29d6")
[controller] 2025-10-24 18:40:32 WARNING ae.runtime.docker_runtime: Failed to remove container ae-echo-rev237-0: 404 Client Error for http+docker://localhost/v1.45/containers/5b16f25d1ca2583488e1290c8aec7e2da16c084e949584d3f64d73a17a7b29d6?v=False&link=False&force=False: Not Found ("No such container: 5b16f25d1ca2583488e1290c8aec7e2da16c084e949584d3f64d73a17a7b29d6")
[controller] 2025-10-24 18:40:32 WARNING ae.runtime.docker_runtime: Failed to stop container ae-echo-rev236-0: 404 Client Error for http+docker://localhost/v1.45/containers/d8b3e926e93a9a4525cfcfc36fc6c9d27ac9271bc16eba0632b1a6eb19197950/stop?t=10: Not Found ("No such container: d8b3e926e93a9a4525cfcfc36fc6c9d27ac9271bc16eba0632b1a6eb19197950")
[controller] 2025-10-24 18:40:32 WARNING ae.runtime.docker_runtime: Failed to remove container ae-echo-rev236-0: 404 Client Error for http+docker://localhost/v1.45/containers/d8b3e926e93a9a4525cfcfc36fc6c9d27ac9271bc16eba0632b1a6eb19197950?v=False&link=False&force=False: Not Found ("No such container: d8b3e926e93a9a4525cfcfc36fc6c9d27ac9271bc16eba0632b1a6eb19197950")
[controller] 2025-10-24 18:40:40 WARNING ae.runtime.docker_runtime: Failed to remove container ae-echo-rev235-0: 409 Client Error for http+docker://localhost/v1.45/containers/6549caed0a7c2b6d0d5556fddc94e5675aeca8555f00ad67d7a48dbe235a1062?v=False&link=False&force=False: Conflict ("removal of container 6549caed0a7c2b6d0d5556fddc94e5675aeca8555f00ad67d7a48dbe235a1062 is already in progress")
[caddy] {"level":"info","ts":1761356441.2922168,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"60532","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356441.2923124,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356441.2923145,"logger":"admin.api","msg":"load complete"}
[sites]
[sites] ==> state/caddy/green.caddy <==
[sites] https://green.home.arpa {
[sites]     log {
[sites]         output stdout
[sites]         format console
[sites]     }
[sites]     # Ensure upstream HSTS does not stick during dev
[sites]     header -Strict-Transport-Security
[sites]     reverse_proxy host.docker.internal:33178 {
[sites]
[sites]         lb_policy first
[sites]     }
[sites] }
[caddy] {"level":"info","ts":1761356441.5546098,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"60538","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356441.5547614,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356441.5547667,"logger":"admin.api","msg":"load complete"}
[controller] Traceback (most recent call last):
[sites]
[sites] ==> state/caddy/echo-mr.caddy <==
[sites] https://echo-mr.home.arpa {
[sites]     log {
[sites]         output stdout
[sites]         format console
[sites]     }
[sites]     # Ensure upstream HSTS does not stick during dev
[sites]     header -Strict-Transport-Security
[sites]     reverse_proxy host.docker.internal:33187 host.docker.internal:33186 host.docker.internal:33185 {
[sites]
[sites]         lb_policy first
[sites]     }
[sites] }
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
[controller] Applied echo: +1/~0/-0, ready=0, live=1, rev=236(progressing)
[controller] Applied echo-stateful: +0/~0/-0, ready=1, live=1, rev=1(ready)
[controller] Applied echo-del: +0/~0/-0, ready=1, live=1, rev=1(ready)
[controller] Applied echo: +1/~0/-0, ready=1, live=1, rev=238(ready)
[caddy] {"level":"info","ts":1761356441.8147466,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"60548","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356441.814905,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356441.8149095,"logger":"admin.api","msg":"load complete"}
[caddy] {"level":"info","ts":1761356442.2553241,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"60560","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356442.2554765,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356442.2554812,"logger":"admin.api","msg":"load complete"}
[caddy] {"level":"info","ts":1761356442.821158,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"60562","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356442.8213115,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356442.821317,"logger":"admin.api","msg":"load complete"}
[caddy] {"level":"info","ts":1761356443.038033,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"60576","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356443.0381944,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356443.0381992,"logger":"admin.api","msg":"load complete"}
[caddy] {"level":"info","ts":1761356443.2476285,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"60582","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356443.2477808,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356443.2477863,"logger":"admin.api","msg":"load complete"}
[caddy] {"level":"info","ts":1761356443.7813387,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"60590","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356443.781492,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356443.7814963,"logger":"admin.api","msg":"load complete"}
[controller] 2025-10-24 18:41:13 INFO __main__: http api listening on port 43453
[controller] 2025-10-24 18:41:13 WARNING __main__: watchdog not available; falling back to interval polling
[sites]
[sites] ==> state/caddy/blue.caddy <==
[sites] https://blue.home.arpa {
[sites]     log {
[sites]         output stdout
[sites]         format console
[sites]     }
[sites]     # Ensure upstream HSTS does not stick during dev
[sites]     header -Strict-Transport-Security
[sites]     reverse_proxy host.docker.internal:33176 {
[sites]
[sites]         lb_policy first
[sites]     }
[sites] }
[caddy] {"level":"info","ts":1761356473.9955623,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"49788","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356473.995714,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356473.9957192,"logger":"admin.api","msg":"load complete"}
[caddy] {"level":"info","ts":1761356474.5663934,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"49800","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356474.5665472,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356474.5665526,"logger":"admin.api","msg":"load complete"}
[caddy] {"level":"info","ts":1761356474.8342178,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"49802","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356474.8343723,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356474.834377,"logger":"admin.api","msg":"load complete"}
[caddy] {"level":"info","ts":1761356475.0741534,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"49812","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356475.074333,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356475.0743408,"logger":"admin.api","msg":"load complete"}
[caddy] {"level":"info","ts":1761356476.123504,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"49822","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356476.1236563,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356476.1236613,"logger":"admin.api","msg":"load complete"}
[caddy] {"level":"info","ts":1761356476.192326,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"49826","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356476.192481,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356476.192487,"logger":"admin.api","msg":"load complete"}
[sites]
[sites] ==> state/caddy/green.caddy <==
[sites] https://green.home.arpa {
[sites]     log {
[sites]         output stdout
[sites]         format console
[sites]     }
[sites]     # Ensure upstream HSTS does not stick during dev
[sites]     header -Strict-Transport-Security
[sites]     reverse_proxy host.docker.internal:33178 {
[sites]
[sites]         lb_policy first
[sites]     }
[sites] }
[caddy] {"level":"info","ts":1761356476.4284878,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"49834","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356476.4286592,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356476.4286654,"logger":"admin.api","msg":"load complete"}
[sites]
[sites] ==> state/caddy/echo-mr.caddy <==
[sites] https://echo-mr.home.arpa {
[sites]     log {
[sites]         output stdout
[sites]         format console
[sites]     }
[sites]     # Ensure upstream HSTS does not stick during dev
[sites]     header -Strict-Transport-Security
[sites]     reverse_proxy host.docker.internal:33187 host.docker.internal:33186 host.docker.internal:33185 {
[sites]
[sites]         lb_policy first
[sites]     }
[sites] }
