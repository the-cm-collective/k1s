[init-demo] Configuring hosts entries
[init-demo] Controller already running (pid 3013113)
[init-demo] Applying demo manifests
[init-demo] Controller supervisor not running; starting it now
2025-10-24 18:46:34 DEBUG docker.utils.config: Trying paths: ['/home/m4xx3d0ut/.docker/config.json', '/home/m4xx3d0ut/.dockercfg']
2025-10-24 18:46:34 DEBUG docker.utils.config: Found file at path: /home/m4xx3d0ut/.docker/config.json
2025-10-24 18:46:34 DEBUG docker.auth: Found 'credHelpers' section
2025-10-24 18:46:34 DEBUG urllib3.connectionpool: http://localhost:None "GET /version HTTP/1.1" 200 808
2025-10-24 18:46:34 DEBUG urllib3.connectionpool: http://localhost:None "GET /v1.45/containers/json?limit=-1&all=1&size=0&trunc_cmd=0&filters=%7B%22label%22%3A+%5B%22ae.app%3Dblue%22%5D%7D HTTP/1.1" 200 1431
2025-10-24 18:46:34 DEBUG urllib3.connectionpool: http://localhost:None "GET /v1.45/containers/62e70ad6c3113d7374c15774a595a2217dd4506e511f2434a63628d5d7ff398a/json HTTP/1.1" 200 None
2025-10-24 18:46:34 DEBUG urllib3.connectionpool: http://localhost:None "GET /v1.45/containers/62e70ad6c3113d7374c15774a595a2217dd4506e511f2434a63628d5d7ff398a/json HTTP/1.1" 200 None
2025-10-24 18:46:34 DEBUG urllib3.connectionpool: http://localhost:None "GET /v1.45/containers/json?limit=-1&all=1&size=0&trunc_cmd=0&filters=%7B%22label%22%3A+%5B%22ae.app%3Dblue%22%5D%7D HTTP/1.1" 200 1431
2025-10-24 18:46:34 DEBUG urllib3.connectionpool: http://localhost:None "GET /v1.45/containers/62e70ad6c3113d7374c15774a595a2217dd4506e511f2434a63628d5d7ff398a/json HTTP/1.1" 200 None
2025-10-24 18:46:34 DEBUG urllib3.connectionpool: http://localhost:None "GET /v1.45/containers/62e70ad6c3113d7374c15774a595a2217dd4506e511f2434a63628d5d7ff398a/json HTTP/1.1" 200 None
2025-10-24 18:46:34 DEBUG ae.ingress.caddy: Wrote Caddy site config to state/caddy/blue.caddy
2025-10-24 18:46:35 DEBUG urllib3.connectionpool: http://localhost:None "GET /v1.45/containers/json?limit=-1&all=1&size=0&trunc_cmd=0&filters=%7B%22label%22%3A+%5B%22ae.app%3Dblue%22%5D%7D HTTP/1.1" 200 1431
2025-10-24 18:46:35 DEBUG urllib3.connectionpool: http://localhost:None "GET /v1.45/containers/62e70ad6c3113d7374c15774a595a2217dd4506e511f2434a63628d5d7ff398a/json HTTP/1.1" 200 None
Applied blue: +0/~0/-0, ready=1, live=1, rev=3(ready)
2025-10-24 18:46:35 DEBUG docker.utils.config: Trying paths: ['/home/m4xx3d0ut/.docker/config.json', '/home/m4xx3d0ut/.dockercfg']
2025-10-24 18:46:35 DEBUG docker.utils.config: Found file at path: /home/m4xx3d0ut/.docker/config.json
2025-10-24 18:46:35 DEBUG docker.auth: Found 'credHelpers' section
2025-10-24 18:46:35 DEBUG urllib3.connectionpool: http://localhost:None "GET /version HTTP/1.1" 200 808
2025-10-24 18:46:35 DEBUG urllib3.connectionpool: http://localhost:None "GET /v1.45/containers/json?limit=-1&all=1&size=0&trunc_cmd=0&filters=%7B%22label%22%3A+%5B%22ae.app%3Dgreen%22%5D%7D HTTP/1.1" 200 1436
2025-10-24 18:46:35 DEBUG urllib3.connectionpool: http://localhost:None "GET /v1.45/containers/9cddf805692a8faf15c3d93fd0f58cd70008a21a99902ab2f70a2fc8aa8be7fe/json HTTP/1.1" 200 None
2025-10-24 18:46:35 DEBUG urllib3.connectionpool: http://localhost:None "GET /v1.45/containers/9cddf805692a8faf15c3d93fd0f58cd70008a21a99902ab2f70a2fc8aa8be7fe/json HTTP/1.1" 200 None
2025-10-24 18:46:35 DEBUG urllib3.connectionpool: http://localhost:None "GET /v1.45/containers/json?limit=-1&all=1&size=0&trunc_cmd=0&filters=%7B%22label%22%3A+%5B%22ae.app%3Dgreen%22%5D%7D HTTP/1.1" 200 1436
2025-10-24 18:46:35 DEBUG urllib3.connectionpool: http://localhost:None "GET /v1.45/containers/9cddf805692a8faf15c3d93fd0f58cd70008a21a99902ab2f70a2fc8aa8be7fe/json HTTP/1.1" 200 None
2025-10-24 18:46:35 DEBUG urllib3.connectionpool: http://localhost:None "GET /v1.45/containers/9cddf805692a8faf15c3d93fd0f58cd70008a21a99902ab2f70a2fc8aa8be7fe/json HTTP/1.1" 200 None
2025-10-24 18:46:35 DEBUG ae.ingress.caddy: Wrote Caddy site config to state/caddy/green.caddy
2025-10-24 18:46:35 DEBUG urllib3.connectionpool: http://localhost:None "GET /v1.45/containers/json?limit=-1&all=1&size=0&trunc_cmd=0&filters=%7B%22label%22%3A+%5B%22ae.app%3Dgreen%22%5D%7D HTTP/1.1" 200 1436
2025-10-24 18:46:35 DEBUG urllib3.connectionpool: http://localhost:None "GET /v1.45/containers/9cddf805692a8faf15c3d93fd0f58cd70008a21a99902ab2f70a2fc8aa8be7fe/json HTTP/1.1" 200 None
Applied green: +0/~0/-0, ready=1, live=1, rev=3(ready)
[init-demo] Building static docs (docs/site)
[init-demo] Starting docs server on http://127.0.0.1:9109 (background)
[init-demo] Current status
blue: desired=1, ready=1, live=1, rev=3(ready), image=demo-blue:latest, ops=+0/~0/-0, ingress=blue.home.arpa/
echo: desired=1, ready=0, live=1, rev=278(progressing), image=alpine:3.20, ops=+1/~0/-0
echo-del: desired=1, ready=1, live=1, rev=1(ready), image=alpine:3.20, ops=+0/~0/-0
echo-mr: desired=3, ready=0, live=3, rev=2(progressing), image=demo-blue:latest, ops=+3/~0/-0, ingress=echo-mr.home.arpa/
echo-stateful: desired=1, ready=1, live=1, rev=1(ready), image=alpine:3.20, ops=+0/~0/-0
green: desired=1, ready=1, live=1, rev=3(ready), image=demo-green:latest, ops=+0/~0/-0, ingress=green.home.arpa/

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
[controller] Applied echo-del: +0/~0/-0, ready=1, live=1, rev=1(ready)
[controller] Applied echo: +0/~0/-0, ready=1, live=1, rev=257(ready)
[controller] Applied echo: +1/~0/-0, ready=0, live=1, rev=258(progressing)
[controller] Applied green: +0/~0/-0, ready=1, live=1, rev=3(ready)
[controller] Applied echo-mr: +0/~0/-0, ready=3, live=3, rev=2(ready)
[controller] Applied blue: +0/~0/-0, ready=1, live=1, rev=3(ready)
[controller] Applied echo: +1/~0/-0, ready=0, live=1, rev=259(progressing)
[controller] Applied echo-stateful: +0/~0/-0, ready=1, live=1, rev=1(ready)
[controller] Applied echo-del: +0/~0/-0, ready=1, live=1, rev=1(ready)
[controller] Applied echo: +1/~0/-0, ready=1, live=1, rev=260(ready)
[controller] Applied echo: +1/~0/-0, ready=0, live=1, rev=261(progressing)
[controller] Applied green: +0/~0/-0, ready=1, live=1, rev=3(ready)
[controller] Applied echo-mr: +0/~0/-0, ready=3, live=3, rev=2(ready)
[controller] Applied blue: +0/~0/-0, ready=1, live=1, rev=3(ready)
[controller] Applied echo: +1/~0/-0, ready=0, live=1, rev=264(progressing)
[controller] Applied echo-stateful: +0/~0/-0, ready=1, live=1, rev=1(ready)
[sites] ==> state/caddy/api.caddy <==
[controller] Applied echo-del: +0/~0/-0, ready=1, live=1, rev=1(ready)
[sites]
[sites] ==> state/caddy/blue.caddy <==
[sites]
[controller] Applied echo: +1/~0/-0, ready=1, live=1, rev=265(ready)
[sites] ==> state/caddy/docs.caddy <==
[sites]
[controller] Applied echo: +1/~0/-0, ready=0, live=1, rev=266(progressing)
[sites] ==> state/caddy/echo-mr.caddy <==
[sites]
[sites] ==> state/caddy/green.caddy <==
[controller] Applied green: +0/~0/-0, ready=1, live=1, rev=3(ready)
[controller] Applied echo-mr: +0/~0/-0, ready=3, live=3, rev=2(ready)
[controller] Applied blue: +0/~0/-0, ready=1, live=1, rev=3(ready)
[controller] Applied echo: +1/~0/-0, ready=0, live=1, rev=267(progressing)
[controller] Applied echo-stateful: +0/~0/-0, ready=1, live=1, rev=1(ready)
[controller] Applied echo-del: +0/~0/-0, ready=1, live=1, rev=1(ready)
[controller] Applied echo: +1/~0/-0, ready=1, live=1, rev=268(ready)
[controller] Applied echo: +1/~0/-0, ready=0, live=1, rev=269(progressing)
[controller] Applied green: +0/~0/-0, ready=1, live=1, rev=3(ready)
[controller] Applied echo-mr: +0/~0/-0, ready=3, live=3, rev=2(ready)
[controller] Applied blue: +0/~0/-0, ready=1, live=1, rev=3(ready)
[controller] Applied echo: +1/~0/-0, ready=0, live=1, rev=270(progressing)
[controller] Applied echo-stateful: +0/~0/-0, ready=1, live=1, rev=1(ready)
[controller] Applied echo-del: +0/~0/-0, ready=1, live=1, rev=1(ready)
[controller] Applied echo: +0/~0/-0, ready=1, live=1, rev=271(ready)
[controller] Applied echo: +1/~0/-0, ready=0, live=1, rev=272(progressing)
[controller] Applied green: +0/~0/-0, ready=1, live=1, rev=3(ready)
[controller] Applied echo-mr: +0/~0/-0, ready=3, live=3, rev=2(ready)
[controller] Applied blue: +0/~0/-0, ready=1, live=1, rev=3(ready)
[controller] Applied echo: +1/~0/-0, ready=0, live=1, rev=273(progressing)
[controller] Applied echo-stateful: +0/~0/-0, ready=1, live=1, rev=1(ready)
[controller] Applied echo-del: +0/~0/-0, ready=1, live=1, rev=1(ready)
[controller] Applied echo: +1/~0/-0, ready=1, live=1, rev=274(ready)
[controller] Applied echo: +1/~0/-0, ready=0, live=1, rev=275(progressing)
[controller] Applied green: +1/~0/-0, ready=1, live=1, rev=3(ready)
[controller] Applied echo-mr: +3/~0/-0, ready=0, live=3, rev=2(progressing)
[controller] 2025-10-24 18:46:33 INFO __main__: http api listening on port 9108
[controller] 2025-10-24 18:46:33 INFO __main__: watchdog not available; falling back to interval polling
[controller] 2025-10-24 18:46:33 INFO ae.ingress.caddy: Caddy container dev-caddy-1 not available yet; skipping reload
[controller] 2025-10-24 18:46:34 INFO __main__: http api listening on port 37083
[controller] 2025-10-24 18:46:34 INFO __main__: watchdog not available; falling back to interval polling
[prometheus] ts=2025-10-25T01:46:34.447Z caller=main.go:589 level=info msg="No time or size retention was set so using the default time retention" duration=15d
[prometheus] ts=2025-10-25T01:46:34.447Z caller=main.go:633 level=info msg="Starting Prometheus Server" mode=server version="(version=2.53.0, branch=HEAD, revision=4c35b9250afefede41c5f5acd76191f90f625898)"
[prometheus] ts=2025-10-25T01:46:34.447Z caller=main.go:638 level=info build_context="(go=go1.22.4, platform=linux/amd64, user=root@7f8d89cbbd64, date=20240619-07:39:12, tags=netgo,builtinassets,stringlabels)"
[prometheus] ts=2025-10-25T01:46:34.447Z caller=main.go:639 level=info host_details="(Linux 6.12.38+kali-amd64 #1 SMP PREEMPT_DYNAMIC Kali 6.12.38-1kali1 (2025-08-12) x86_64 6ee135d1bb7c (none))"
[prometheus] ts=2025-10-25T01:46:34.447Z caller=main.go:640 level=info fd_limits="(soft=524288, hard=524288)"
[prometheus] ts=2025-10-25T01:46:34.447Z caller=main.go:641 level=info vm_limits="(soft=unlimited, hard=unlimited)"
[prometheus] ts=2025-10-25T01:46:34.448Z caller=web.go:568 level=info component=web msg="Start listening for connections" address=0.0.0.0:9090
[prometheus] ts=2025-10-25T01:46:34.449Z caller=main.go:1148 level=info msg="Starting TSDB ..."
[prometheus] ts=2025-10-25T01:46:34.451Z caller=tls_config.go:313 level=info component=web msg="Listening on" address=[::]:9090
[prometheus] ts=2025-10-25T01:46:34.451Z caller=tls_config.go:316 level=info component=web msg="TLS is disabled." http2=false address=[::]:9090
[prometheus] ts=2025-10-25T01:46:34.452Z caller=head.go:626 level=info component=tsdb msg="Replaying on-disk memory mappable chunks if any"
[prometheus] ts=2025-10-25T01:46:34.452Z caller=head.go:713 level=info component=tsdb msg="On-disk memory mappable chunks replay completed" duration=1.482µs
[prometheus] ts=2025-10-25T01:46:34.452Z caller=head.go:721 level=info component=tsdb msg="Replaying WAL, this may take a while"
[prometheus] ts=2025-10-25T01:46:34.452Z caller=head.go:793 level=info component=tsdb msg="WAL segment loaded" segment=0 maxSegment=0
[prometheus] ts=2025-10-25T01:46:34.452Z caller=head.go:830 level=info component=tsdb msg="WAL replay completed" checkpoint_replay_duration=26.077µs wal_replay_duration=201.872µs wbl_replay_duration=176ns chunk_snapshot_load_duration=0s mmap_chunk_replay_duration=1.482µs total_replay_duration=246.835µs
[prometheus] ts=2025-10-25T01:46:34.453Z caller=main.go:1169 level=info fs_type=EXT4_SUPER_MAGIC
[prometheus] ts=2025-10-25T01:46:34.453Z caller=main.go:1172 level=info msg="TSDB started"
[prometheus] ts=2025-10-25T01:46:34.453Z caller=main.go:1354 level=info msg="Loading configuration file" filename=/etc/prometheus/prometheus.yml
[prometheus] ts=2025-10-25T01:46:34.453Z caller=main.go:1391 level=info msg="updated GOGC" old=100 new=75
[prometheus] ts=2025-10-25T01:46:34.453Z caller=main.go:1402 level=info msg="Completed loading of configuration file" filename=/etc/prometheus/prometheus.yml totalDuration=245.14µs db_storage=941ns remote_storage=1.307µs web_handler=99ns query_engine=827ns scrape=111.369µs scrape_sd=10.922µs notify=623ns notify_sd=500ns rules=1.213µs tracing=4.11µs
[prometheus] ts=2025-10-25T01:46:34.453Z caller=main.go:1133 level=info msg="Server is ready to receive web requests."
[prometheus] ts=2025-10-25T01:46:34.453Z caller=manager.go:164 level=info component="rule manager" msg="Starting rule manager..."
[caddy] {"level":"info","ts":1761356794.4849374,"msg":"using config from file","file":"/etc/caddy/Caddyfile"}
[caddy] {"level":"warn","ts":1761356794.4849877,"msg":"No files matching import glob pattern","pattern":"sites/*"}
[caddy] {"level":"info","ts":1761356794.4857507,"msg":"adapted config to JSON","adapter":"caddyfile"}
[caddy] {"level":"warn","ts":1761356794.4857552,"msg":"Caddyfile input is not formatted; run 'caddy fmt --overwrite' to fix inconsistencies","adapter":"caddyfile","file":"/etc/caddy/Caddyfile","line":3}
[caddy] {"level":"info","ts":1761356794.4863641,"logger":"admin","msg":"admin endpoint started","address":"localhost:2019","enforce_origin":false,"origins":["//127.0.0.1:2019","//localhost:2019","//[::1]:2019"]}
[caddy] {"level":"info","ts":1761356794.486451,"logger":"http.auto_https","msg":"server is listening only on the HTTPS port but has no TLS connection policies; adding one to enable TLS","server_name":"srv0","https_port":443}
[caddy] {"level":"info","ts":1761356794.486459,"logger":"http.auto_https","msg":"automatic HTTP->HTTPS redirects are disabled","server_name":"srv0"}
[caddy] {"level":"info","ts":1761356794.4865165,"logger":"tls.cache.maintenance","msg":"started background certificate maintenance","cache":"0xc00055c880"}
[caddy] {"level":"warn","ts":1761356794.4898808,"logger":"pki.ca.local","msg":"installing root certificate (you might be prompted for password)","path":"storage:pki/authorities/local/root.crt"}
[caddy] {"level":"info","ts":1761356794.4910197,"msg":"warning: \"certutil\" is not available, install \"certutil\" with \"apt install libnss3-tools\" or \"yum install nss-tools\" and try again"}
[caddy] {"level":"info","ts":1761356794.4910328,"msg":"define JAVA_HOME environment variable to use the Java trust"}
[caddy] {"level":"info","ts":1761356794.491727,"logger":"tls","msg":"cleaning storage unit","storage":"FileStorage:/data/caddy"}
[caddy] {"level":"info","ts":1761356794.491902,"logger":"tls","msg":"finished cleaning storage units"}
[caddy] {"level":"info","ts":1761356794.5219703,"msg":"certificate installed properly in linux trusts"}
[caddy] {"level":"info","ts":1761356794.5221865,"logger":"http","msg":"enabling HTTP/3 listener","addr":":443"}
[caddy] {"level":"info","ts":1761356794.522273,"msg":"failed to sufficiently increase receive buffer size (was: 208 kiB, wanted: 7168 kiB, got: 416 kiB). See https://github.com/quic-go/quic-go/wiki/UDP-Buffer-Sizes for details."}
[caddy] {"level":"info","ts":1761356794.522381,"logger":"http.log","msg":"server running","name":"srv0","protocols":["h1","h2","h3"]}
[caddy] {"level":"info","ts":1761356794.522391,"logger":"http","msg":"enabling automatic TLS certificate management","domains":["green.home.arpa","blue.home.arpa","docs.home.arpa","api.home.arpa","echo-mr.home.arpa"]}
[caddy] {"level":"info","ts":1761356794.522885,"logger":"tls.obtain","msg":"acquiring lock","identifier":"api.home.arpa"}
[caddy] {"level":"info","ts":1761356794.5229297,"logger":"tls.obtain","msg":"acquiring lock","identifier":"green.home.arpa"}
[caddy] {"level":"info","ts":1761356794.5232089,"logger":"tls.obtain","msg":"acquiring lock","identifier":"blue.home.arpa"}
[caddy] {"level":"info","ts":1761356794.5233061,"logger":"tls.obtain","msg":"acquiring lock","identifier":"docs.home.arpa"}
[caddy] {"level":"info","ts":1761356794.5235739,"logger":"tls.obtain","msg":"acquiring lock","identifier":"echo-mr.home.arpa"}
[caddy] {"level":"info","ts":1761356794.524558,"logger":"tls.obtain","msg":"lock acquired","identifier":"api.home.arpa"}
[caddy] {"level":"info","ts":1761356794.5245955,"logger":"tls.obtain","msg":"lock acquired","identifier":"green.home.arpa"}
[caddy] {"level":"info","ts":1761356794.5245972,"logger":"tls.obtain","msg":"lock acquired","identifier":"blue.home.arpa"}
[caddy] {"level":"info","ts":1761356794.524634,"logger":"tls.obtain","msg":"obtaining certificate","identifier":"api.home.arpa"}
[caddy] {"level":"info","ts":1761356794.5246513,"logger":"tls.obtain","msg":"obtaining certificate","identifier":"green.home.arpa"}
[caddy] {"level":"info","ts":1761356794.524657,"logger":"tls.obtain","msg":"obtaining certificate","identifier":"blue.home.arpa"}
[caddy] {"level":"info","ts":1761356794.5246797,"logger":"tls.obtain","msg":"lock acquired","identifier":"docs.home.arpa"}
[caddy] {"level":"info","ts":1761356794.5247502,"logger":"tls.obtain","msg":"obtaining certificate","identifier":"docs.home.arpa"}
[caddy] {"level":"info","ts":1761356794.5259688,"msg":"autosaved config (load with --resume flag)","file":"/config/caddy/autosave.json"}
[caddy] {"level":"info","ts":1761356794.5259893,"msg":"serving initial configuration"}
[caddy] {"level":"info","ts":1761356794.5262032,"logger":"tls.obtain","msg":"lock acquired","identifier":"echo-mr.home.arpa"}
[caddy] {"level":"info","ts":1761356794.526273,"logger":"tls.obtain","msg":"obtaining certificate","identifier":"echo-mr.home.arpa"}
[caddy] {"level":"info","ts":1761356794.5264723,"logger":"tls.obtain","msg":"certificate obtained successfully","identifier":"blue.home.arpa","issuer":"local"}
[caddy] {"level":"info","ts":1761356794.526538,"logger":"tls.obtain","msg":"releasing lock","identifier":"blue.home.arpa"}
[caddy] {"level":"warn","ts":1761356794.5268114,"logger":"tls","msg":"stapling OCSP","error":"no OCSP stapling for [blue.home.arpa]: no OCSP server specified in certificate","identifiers":["blue.home.arpa"]}
[caddy] {"level":"info","ts":1761356794.5270016,"logger":"tls.obtain","msg":"certificate obtained successfully","identifier":"api.home.arpa","issuer":"local"}
[caddy] {"level":"info","ts":1761356794.527058,"logger":"tls.obtain","msg":"releasing lock","identifier":"api.home.arpa"}
[caddy] {"level":"info","ts":1761356794.5271623,"logger":"tls.obtain","msg":"certificate obtained successfully","identifier":"green.home.arpa","issuer":"local"}
[caddy] {"level":"info","ts":1761356794.5271933,"logger":"tls.obtain","msg":"certificate obtained successfully","identifier":"docs.home.arpa","issuer":"local"}
[caddy] {"level":"info","ts":1761356794.5272474,"logger":"tls.obtain","msg":"releasing lock","identifier":"green.home.arpa"}
[caddy] {"level":"warn","ts":1761356794.5273192,"logger":"tls","msg":"stapling OCSP","error":"no OCSP stapling for [api.home.arpa]: no OCSP server specified in certificate","identifiers":["api.home.arpa"]}
[caddy] {"level":"info","ts":1761356794.527252,"logger":"tls.obtain","msg":"releasing lock","identifier":"docs.home.arpa"}
[caddy] {"level":"info","ts":1761356794.5273762,"logger":"tls.obtain","msg":"certificate obtained successfully","identifier":"echo-mr.home.arpa","issuer":"local"}
[caddy] {"level":"info","ts":1761356794.5274704,"logger":"tls.obtain","msg":"releasing lock","identifier":"echo-mr.home.arpa"}
[caddy] {"level":"warn","ts":1761356794.527626,"logger":"tls","msg":"stapling OCSP","error":"no OCSP stapling for [docs.home.arpa]: no OCSP server specified in certificate","identifiers":["docs.home.arpa"]}
[caddy] {"level":"warn","ts":1761356794.5277472,"logger":"tls","msg":"stapling OCSP","error":"no OCSP stapling for [echo-mr.home.arpa]: no OCSP server specified in certificate","identifiers":["echo-mr.home.arpa"]}
[caddy] {"level":"warn","ts":1761356794.5280135,"logger":"tls","msg":"stapling OCSP","error":"no OCSP stapling for [green.home.arpa]: no OCSP server specified in certificate","identifiers":["green.home.arpa"]}
[caddy] {"level":"info","ts":1761356794.9144497,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"34534","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356794.914649,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356794.9146543,"logger":"admin.api","msg":"load complete"}
[caddy] {"level":"info","ts":1761356794.9937482,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"34538","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356794.9938483,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356794.993851,"logger":"admin.api","msg":"load complete"}
[caddy] {"level":"info","ts":1761356795.4013913,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"34546","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356795.401565,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356795.4015698,"logger":"admin.api","msg":"load complete"}
[caddy] 2025/10/25 01:46:35.703 INFO    http.log.access.log0    handled request{"request": {"remote_ip": "172.26.0.1", "remote_port": "59770", "client_ip": "172.26.0.1", "proto": "HTTP/2.0", "method": "GET", "host": "api.home.arpa:8443", "uri": "/openapi.json", "headers": {"User-Agent": ["curl/8.15.0"], "Accept": ["*/*"]}, "tls": {"resumed": false, "version": 772, "cipher_suite": 4865, "proto": "h2", "server_name": "api.home.arpa"}}, "bytes_read": 0, "user_id": "", "duration": 0.000949232, "size": 3393, "status": 200, "resp_headers": {"Server": ["Caddy", "BaseHTTP/0.6 Python/3.13.7"], "Alt-Svc": ["h3=\":443\"; ma=2592000"], "Date": ["Sat, 25 Oct 2025 01:46:35 GMT"], "Content-Type": ["application/json"], "Content-Length": ["3393"]}}
[controller] 2025-10-24 18:46:45 WARNING ae.runtime.docker_runtime: Failed to remove container ae-echo-rev278-0: 409 Client Error for http+docker://localhost/v1.45/containers/325c82ade9b2a669105a18d14a754ae8bd8cca64e27919f1903fca39ccd10743?v=False&link=False&force=False: Conflict ("removal of container 325c82ade9b2a669105a18d14a754ae8bd8cca64e27919f1903fca39ccd10743 is already in progress")
[controller] 2025-10-24 18:46:56 WARNING ae.runtime.docker_runtime: Failed to remove container ae-echo-rev276-0: 409 Client Error for http+docker://localhost/v1.45/containers/1472d77426083a075965a989b616d3362b83f041cac0b806004d17e2df267378?v=False&link=False&force=False: Conflict ("removal of container 1472d77426083a075965a989b616d3362b83f041cac0b806004d17e2df267378 is already in progress")
[controller] 2025-10-24 18:47:07 WARNING ae.runtime.docker_runtime: Failed to remove container ae-echo-rev275-0: 409 Client Error for http+docker://localhost/v1.45/containers/17817a6953453e237db1f5ea9ada4bdda1c459f60a48a823494fa5b3751b14d4?v=False&link=False&force=False: Conflict ("removal of container 17817a6953453e237db1f5ea9ada4bdda1c459f60a48a823494fa5b3751b14d4 is already in progress")
[sites] https://green.home.arpa {
[sites]     log {
[sites]         output stdout
[sites]         format console
[sites]     }
[sites]     # Ensure upstream HSTS does not stick during dev
[sites]     header -Strict-Transport-Security
[sites]     reverse_proxy host.docker.internal:33349 {
[sites]
[sites]         lb_policy first
[sites]     }
[sites] }
[caddy] {"level":"info","ts":1761356827.5740361,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"45668","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356827.574184,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356827.574189,"logger":"admin.api","msg":"load complete"}
[sites]
[sites] ==> state/caddy/echo-mr.caddy <==
[sites] https://echo-mr.home.arpa {
[sites]     log {
[sites]         output stdout
[sites]         format console
[sites]     }
[sites]     # Ensure upstream HSTS does not stick during dev
[sites]     header -Strict-Transport-Security
[sites]     reverse_proxy host.docker.internal:33352 host.docker.internal:33351 host.docker.internal:33350 {
[sites]
[sites]         lb_policy first
[sites]     }
[sites] }
[controller] Traceback (most recent call last):
[controller]   File "/home/m4xx3d0ut/git/k1s/src/ae/secrets/manager.py", line 71, in _decrypt
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
[controller]   File "/home/m4xx3d0ut/git/k1s/src/ae/secrets/manager.py", line 101, in _decrypt
[controller]     raise RuntimeError(f"sops decrypt failed for {path}: {exc.stderr}") from exc
[controller] RuntimeError: sops decrypt failed for specs/examples/demo-secret.sops.yaml: sops metadata not found
[controller]
[controller] Applied blue: +1/~0/-0, ready=1, live=1, rev=3(ready)
[controller] Applied echo: +1/~0/-0, ready=0, live=1, rev=276(progressing)
[controller] Applied echo-stateful: +1/~0/-0, ready=1, live=1, rev=1(ready)
[controller] Applied echo-del: +1/~0/-0, ready=1, live=1, rev=1(ready)
[controller] Applied echo: +1/~0/-0, ready=1, live=1, rev=277(ready)
[caddy] {"level":"info","ts":1761356827.809373,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"45678","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356827.8098178,"logger":"admin","msg":"admin endpoint started","address":"localhost:2019","enforce_origin":false,"origins":["//localhost:2019","//[::1]:2019","//127.0.0.1:2019"]}
[caddy] {"level":"info","ts":1761356827.8099136,"logger":"http.auto_https","msg":"server is listening only on the HTTPS port but has no TLS connection policies; adding one to enable TLS","server_name":"srv0","https_port":443}
[caddy] {"level":"info","ts":1761356827.8099272,"logger":"http.auto_https","msg":"automatic HTTP->HTTPS redirects are disabled","server_name":"srv0"}
[caddy] {"level":"warn","ts":1761356827.8102539,"logger":"pki.ca.local","msg":"installing root certificate (you might be prompted for password)","path":"storage:pki/authorities/local/root.crt"}
[caddy] {"level":"info","ts":1761356827.8103375,"msg":"warning: \"certutil\" is not available, install \"certutil\" with \"apt install libnss3-tools\" or \"yum install nss-tools\" and try again"}
[caddy] {"level":"info","ts":1761356827.8103411,"msg":"define JAVA_HOME environment variable to use the Java trust"}
[caddy] {"level":"info","ts":1761356827.8308122,"msg":"certificate installed properly in linux trusts"}
[caddy] {"level":"info","ts":1761356827.8309069,"logger":"http","msg":"enabling HTTP/3 listener","addr":":443"}
[caddy] {"level":"info","ts":1761356827.8309283,"logger":"http.log","msg":"server running","name":"srv0","protocols":["h1","h2","h3"]}
[caddy] {"level":"info","ts":1761356827.8309317,"logger":"http","msg":"enabling automatic TLS certificate management","domains":["blue.home.arpa","docs.home.arpa","api.home.arpa","echo-mr.home.arpa","green.home.arpa"]}
[caddy] {"level":"info","ts":1761356827.8309436,"logger":"http","msg":"servers shutting down with eternal grace period"}
[caddy] {"level":"info","ts":1761356827.8310895,"msg":"autosaved config (load with --resume flag)","file":"/config/caddy/autosave.json"}
[caddy] {"level":"info","ts":1761356827.8311021,"logger":"admin.api","msg":"load complete"}
[caddy] {"level":"info","ts":1761356827.845278,"logger":"admin","msg":"stopped previous server","address":"localhost:2019"}
[caddy] 2025/10/25 01:47:31.381 INFO    http.log.access.log2    handled request{"request": {"remote_ip": "172.26.0.1", "remote_port": "50084", "client_ip": "172.26.0.1", "proto": "HTTP/2.0", "method": "GET", "host": "docs.home.arpa:8443", "uri": "/", "headers": {"Cache-Control": ["max-age=0"], "Sec-Ch-Ua": ["\"Chromium\";v=\"140\", \"Not=A?Brand\";v=\"24\", \"Google Chrome\";v=\"140\""], "Sec-Ch-Ua-Platform": ["\"Linux\""], "Accept-Language": ["en-US,en;q=0.9"], "Sec-Fetch-Dest": ["document"], "Sec-Ch-Ua-Mobile": ["?0"], "Accept": ["text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7"], "Sec-Fetch-Mode": ["navigate"], "Accept-Encoding": ["gzip, deflate, br, zstd"], "Upgrade-Insecure-Requests": ["1"], "User-Agent": ["Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"], "Sec-Fetch-Site": ["none"], "Sec-Fetch-User": ["?1"], "Priority": ["u=0, i"]}, "tls": {"resumed": false, "version": 772, "cipher_suite": 4865, "proto": "h2", "server_name": "docs.home.arpa"}}, "bytes_read": 0, "user_id": "", "duration": 0.003830371, "size": 4424, "status": 200, "resp_headers": {"Server": ["Caddy", "SimpleHTTP/0.6 Python/3.13.7"], "Alt-Svc": ["h3=\":443\"; ma=2592000"], "Date": ["Sat, 25 Oct 2025 01:47:31 GMT"], "Content-Type": ["text/html"], "Content-Length": ["4424"], "Last-Modified": ["Sat, 25 Oct 2025 01:46:35 GMT"]}}
[caddy] 2025/10/25 01:47:31.605 INFO    http.log.access.log2    handled request{"request": {"remote_ip": "172.26.0.1", "remote_port": "50084", "client_ip": "172.26.0.1", "proto": "HTTP/2.0", "method": "GET", "host": "docs.home.arpa:8443", "uri": "/favicon.ico", "headers": {"User-Agent": ["Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"], "Sec-Fetch-Mode": ["no-cors"], "Sec-Fetch-Dest": ["image"], "Referer": ["https://docs.home.arpa:8443/"], "Accept-Language": ["en-US,en;q=0.9"], "Accept-Encoding": ["gzip, deflate, br, zstd"], "Priority": ["u=1, i"], "Sec-Ch-Ua-Platform": ["\"Linux\""], "Sec-Ch-Ua": ["\"Chromium\";v=\"140\", \"Not=A?Brand\";v=\"24\", \"Google Chrome\";v=\"140\""], "Sec-Ch-Ua-Mobile": ["?0"], "Accept": ["image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8"], "Sec-Fetch-Site": ["same-origin"]}, "tls": {"resumed": false, "version": 772, "cipher_suite": 4865, "proto": "h2", "server_name": "docs.home.arpa"}}, "bytes_read": 0, "user_id": "", "duration": 0.001323848, "size": 335, "status": 404, "resp_headers": {"Server": ["Caddy", "SimpleHTTP/0.6 Python/3.13.7"], "Alt-Svc": ["h3=\":443\"; ma=2592000"], "Date": ["Sat, 25 Oct 2025 01:47:31 GMT"], "Content-Type": ["text/html;charset=utf-8"], "Content-Length": ["335"]}}
[caddy] {"level":"error","ts":1761356856.7458704,"logger":"http.log.error.log0","msg":"dial tcp 172.17.0.1:9108: connect: connection refused","request":{"remote_ip":"172.26.0.1","remote_port":"50118","client_ip":"172.26.0.1","proto":"HTTP/2.0","method":"GET","host":"api.home.arpa:8443","uri":"/redoc","headers":{"Sec-Ch-Ua-Mobile":["?0"],"Sec-Ch-Ua-Platform":["\"Linux\""],"Upgrade-Insecure-Requests":["1"],"Sec-Fetch-Mode":["navigate"],"Priority":["u=0, i"],"Cache-Control":["max-age=0"],"Sec-Ch-Ua":["\"Chromium\";v=\"140\", \"Not=A?Brand\";v=\"24\", \"Google Chrome\";v=\"140\""],"Accept-Language":["en-US,en;q=0.9"],"User-Agent":["Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"],"Sec-Fetch-User":["?1"],"Sec-Fetch-Dest":["document"],"Accept-Encoding":["gzip, deflate, br, zstd"],"Accept":["text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7"],"Sec-Fetch-Site":["cross-site"],"Referer":["https://docs.home.arpa:8443/"]},"tls":{"resumed":false,"version":772,"cipher_suite":4865,"proto":"h2","server_name":"api.home.arpa"}},"duration":0.00026855,"status":502,"err_id":"3zzt143h7","err_trace":"reverseproxy.statusError (reverseproxy.go:1269)"}
[caddy] 2025/10/25 01:47:36.745 ERROR   http.log.access.log0    handled request{"request": {"remote_ip": "172.26.0.1", "remote_port": "50118", "client_ip": "172.26.0.1", "proto": "HTTP/2.0", "method": "GET", "host": "api.home.arpa:8443", "uri": "/redoc", "headers": {"Sec-Ch-Ua-Mobile": ["?0"], "Sec-Ch-Ua-Platform": ["\"Linux\""], "Upgrade-Insecure-Requests": ["1"], "Sec-Fetch-Mode": ["navigate"], "Priority": ["u=0, i"], "Cache-Control": ["max-age=0"], "Sec-Ch-Ua": ["\"Chromium\";v=\"140\", \"Not=A?Brand\";v=\"24\", \"Google Chrome\";v=\"140\""], "Accept-Language": ["en-US,en;q=0.9"], "User-Agent": ["Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"], "Sec-Fetch-User": ["?1"], "Sec-Fetch-Dest": ["document"], "Accept-Encoding": ["gzip, deflate, br, zstd"], "Accept": ["text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7"], "Sec-Fetch-Site": ["cross-site"], "Referer": ["https://docs.home.arpa:8443/"]}, "tls": {"resumed": false, "version": 772, "cipher_suite": 4865, "proto": "h2", "server_name": "api.home.arpa"}}, "bytes_read": 0, "user_id": "", "duration": 0.00026855, "size": 0, "status": 502, "resp_headers": {"Server": ["Caddy"], "Alt-Svc": ["h3=\":443\"; ma=2592000"]}}
[caddy] {"level":"error","ts":1761356858.4576755,"logger":"http.log.error.log0","msg":"dial tcp 172.17.0.1:9108: connect: connection refused","request":{"remote_ip":"172.26.0.1","remote_port":"50118","client_ip":"172.26.0.1","proto":"HTTP/2.0","method":"GET","host":"api.home.arpa:8443","uri":"/redoc","headers":{"Sec-Ch-Ua-Mobile":["?0"],"Sec-Fetch-Site":["cross-site"],"Priority":["u=0, i"],"Accept":["text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7"],"Sec-Fetch-Mode":["navigate"],"Sec-Fetch-User":["?1"],"Accept-Language":["en-US,en;q=0.9"],"Cache-Control":["max-age=0"],"Sec-Ch-Ua":["\"Chromium\";v=\"140\", \"Not=A?Brand\";v=\"24\", \"Google Chrome\";v=\"140\""],"Sec-Ch-Ua-Platform":["\"Linux\""],"Upgrade-Insecure-Requests":["1"],"Sec-Fetch-Dest":["document"],"Accept-Encoding":["gzip, deflate, br, zstd"],"User-Agent":["Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"],"Referer":["https://docs.home.arpa:8443/"]},"tls":{"resumed":false,"version":772,"cipher_suite":4865,"proto":"h2","server_name":"api.home.arpa"}},"duration":0.000321507,"status":502,"err_id":"2zu2u51gx","err_trace":"reverseproxy.statusError (reverseproxy.go:1269)"}
[caddy] 2025/10/25 01:47:38.457 ERROR   http.log.access.log0    handled request{"request": {"remote_ip": "172.26.0.1", "remote_port": "50118", "client_ip": "172.26.0.1", "proto": "HTTP/2.0", "method": "GET", "host": "api.home.arpa:8443", "uri": "/redoc", "headers": {"Sec-Fetch-Mode": ["navigate"], "Sec-Fetch-User": ["?1"], "Accept-Language": ["en-US,en;q=0.9"], "Cache-Control": ["max-age=0"], "Sec-Ch-Ua": ["\"Chromium\";v=\"140\", \"Not=A?Brand\";v=\"24\", \"Google Chrome\";v=\"140\""], "Sec-Ch-Ua-Platform": ["\"Linux\""], "Upgrade-Insecure-Requests": ["1"], "Accept": ["text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7"], "Sec-Fetch-Dest": ["document"], "Accept-Encoding": ["gzip, deflate, br, zstd"], "User-Agent": ["Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"], "Referer": ["https://docs.home.arpa:8443/"], "Sec-Ch-Ua-Mobile": ["?0"], "Sec-Fetch-Site": ["cross-site"], "Priority": ["u=0, i"]}, "tls": {"resumed": false, "version": 772, "cipher_suite": 4865, "proto": "h2", "server_name": "api.home.arpa"}}, "bytes_read": 0, "user_id": "", "duration": 0.000321507, "size": 0, "status": 502, "resp_headers": {"Server": ["Caddy"], "Alt-Svc": ["h3=\":443\"; ma=2592000"]}}
[controller] 2025-10-24 18:47:39 INFO __main__: http api listening on port 9108
[controller] 2025-10-24 18:47:39 INFO __main__: watchdog not available; falling back to interval polling
[sites]
[sites] ==> state/caddy/blue.caddy <==
[sites] https://blue.home.arpa {
[sites]     log {
[sites]         output stdout
[sites]         format console
[sites]     }
[sites]     # Ensure upstream HSTS does not stick during dev
[sites]     header -Strict-Transport-Security
[sites]     reverse_proxy host.docker.internal:33353 {
[sites]
[sites]         lb_policy first
[sites]     }
[sites] }
[caddy] {"level":"info","ts":1761356860.159865,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"42052","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356860.1600509,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356860.1600564,"logger":"admin.api","msg":"load complete"}
[caddy] 2025/10/25 01:47:43.404 INFO    http.log.access.log0    handled request{"request": {"remote_ip": "172.26.0.1", "remote_port": "41740", "client_ip": "172.26.0.1", "proto": "HTTP/2.0", "method": "GET", "host": "api.home.arpa:8443", "uri": "/swagger", "headers": {"Sec-Ch-Ua": ["\"Chromium\";v=\"140\", \"Not=A?Brand\";v=\"24\", \"Google Chrome\";v=\"140\""], "Sec-Fetch-Site": ["cross-site"], "Priority": ["u=0, i"], "User-Agent": ["Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"], "Accept": ["text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7"], "Referer": ["https://docs.home.arpa:8443/"], "Accept-Encoding": ["gzip, deflate, br, zstd"], "Sec-Ch-Ua-Platform": ["\"Linux\""], "Upgrade-Insecure-Requests": ["1"], "Sec-Fetch-Dest": ["document"], "Sec-Ch-Ua-Mobile": ["?0"], "Sec-Fetch-Mode": ["navigate"], "Sec-Fetch-User": ["?1"], "Accept-Language": ["en-US,en;q=0.9"]}, "tls": {"resumed": false, "version": 772, "cipher_suite": 4865, "proto": "h2", "server_name": "api.home.arpa"}}, "bytes_read": 0, "user_id": "", "duration": 0.00082829, "size": 542, "status": 200, "resp_headers": {"Alt-Svc": ["h3=\":443\"; ma=2592000"], "Date": ["Sat, 25 Oct 2025 01:47:43 GMT"], "Content-Type": ["text/html; charset=utf-8"], "Content-Length": ["542"], "Server": ["Caddy", "BaseHTTP/0.6 Python/3.13.7"]}}
[caddy] 2025/10/25 01:47:43.693 INFO    http.log.access.log0    handled request{"request": {"remote_ip": "172.26.0.1", "remote_port": "41770", "client_ip": "172.26.0.1", "proto": "HTTP/2.0", "method": "GET", "host": "api.home.arpa:8443", "uri": "/favicon.ico", "headers": {"User-Agent": ["Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"], "Sec-Ch-Ua": ["\"Chromium\";v=\"140\", \"Not=A?Brand\";v=\"24\", \"Google Chrome\";v=\"140\""], "Sec-Ch-Ua-Mobile": ["?0"], "Sec-Fetch-Site": ["same-origin"], "Accept-Language": ["en-US,en;q=0.9"], "Priority": ["u=1, i"], "Sec-Ch-Ua-Platform": ["\"Linux\""], "Accept": ["image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8"], "Sec-Fetch-Mode": ["no-cors"], "Sec-Fetch-Dest": ["image"], "Referer": ["https://api.home.arpa:8443/swagger"], "Accept-Encoding": ["gzip, deflate, br, zstd"]}, "tls": {"resumed": false, "version": 772, "cipher_suite": 4865, "proto": "h2", "server_name": "api.home.arpa"}}, "bytes_read": 0, "user_id": "", "duration": 0.000769978, "size": 0, "status": 404, "resp_headers": {"Server": ["Caddy", "BaseHTTP/0.6 Python/3.13.7"], "Alt-Svc": ["h3=\":443\"; ma=2592000"], "Date": ["Sat, 25 Oct 2025 01:47:43 GMT"]}}
[caddy] 2025/10/25 01:47:43.694 INFO    http.log.access.log0    handled request{"request": {"remote_ip": "172.26.0.1", "remote_port": "41770", "client_ip": "172.26.0.1", "proto": "HTTP/2.0", "method": "GET", "host": "api.home.arpa:8443", "uri": "/openapi.json", "headers": {"Accept": ["application/json,*/*"], "Sec-Ch-Ua": ["\"Chromium\";v=\"140\", \"Not=A?Brand\";v=\"24\", \"Google Chrome\";v=\"140\""], "Accept-Language": ["en-US,en;q=0.9"], "Priority": ["u=1, i"], "User-Agent": ["Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"], "Sec-Ch-Ua-Mobile": ["?0"], "Sec-Fetch-Site": ["same-origin"], "Sec-Fetch-Mode": ["cors"], "Sec-Fetch-Dest": ["empty"], "Referer": ["https://api.home.arpa:8443/swagger"], "Accept-Encoding": ["gzip, deflate, br, zstd"], "Sec-Ch-Ua-Platform": ["\"Linux\""]}, "tls": {"resumed": false, "version": 772, "cipher_suite": 4865, "proto": "h2", "server_name": "api.home.arpa"}}, "bytes_read": 0, "user_id": "", "duration": 0.001131299, "size": 3393, "status": 200, "resp_headers": {"Date": ["Sat, 25 Oct 2025 01:47:43 GMT"], "Content-Type": ["application/json"], "Content-Length": ["3393"], "Server": ["Caddy", "BaseHTTP/0.6 Python/3.13.7"], "Alt-Svc": ["h3=\":443\"; ma=2592000"]}}
[caddy] 2025/10/25 01:47:49.601 INFO    http.log.access.log0    handled request{"request": {"remote_ip": "172.26.0.1", "remote_port": "41770", "client_ip": "172.26.0.1", "proto": "HTTP/2.0", "method": "GET", "host": "api.home.arpa:8443", "uri": "/redoc", "headers": {"Sec-Ch-Ua": ["\"Chromium\";v=\"140\", \"Not=A?Brand\";v=\"24\", \"Google Chrome\";v=\"140\""], "Upgrade-Insecure-Requests": ["1"], "Referer": ["https://docs.home.arpa:8443/"], "Accept-Encoding": ["gzip, deflate, br, zstd"], "Sec-Ch-Ua-Mobile": ["?0"], "User-Agent": ["Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"], "Accept": ["text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7"], "Sec-Fetch-User": ["?1"], "Sec-Fetch-Dest": ["document"], "Priority": ["u=0, i"], "Sec-Ch-Ua-Platform": ["\"Linux\""], "Accept-Language": ["en-US,en;q=0.9"], "Sec-Fetch-Site": ["cross-site"], "Sec-Fetch-Mode": ["navigate"]}, "tls": {"resumed": false, "version": 772, "cipher_suite": 4865, "proto": "h2", "server_name": "api.home.arpa"}}, "bytes_read": 0, "user_id": "", "duration": 0.000682755, "size": 502, "status": 200, "resp_headers": {"Alt-Svc": ["h3=\":443\"; ma=2592000"], "Date": ["Sat, 25 Oct 2025 01:47:49 GMT"], "Content-Type": ["text/html; charset=utf-8"], "Content-Length": ["502"], "Server": ["Caddy", "BaseHTTP/0.6 Python/3.13.7"]}}
[caddy] 2025/10/25 01:47:49.734 INFO    http.log.access.log0    handled request{"request": {"remote_ip": "172.26.0.1", "remote_port": "41770", "client_ip": "172.26.0.1", "proto": "HTTP/2.0", "method": "GET", "host": "api.home.arpa:8443", "uri": "/openapi.json", "headers": {"Sec-Fetch-Mode": ["cors"], "Sec-Fetch-Dest": ["empty"], "Accept-Language": ["en-US,en;q=0.9"], "Priority": ["u=1, i"], "Sec-Ch-Ua-Platform": ["\"Linux\""], "User-Agent": ["Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"], "Sec-Ch-Ua": ["\"Chromium\";v=\"140\", \"Not=A?Brand\";v=\"24\", \"Google Chrome\";v=\"140\""], "Sec-Ch-Ua-Mobile": ["?0"], "Accept": ["*/*"], "Sec-Fetch-Site": ["same-origin"], "Referer": ["https://api.home.arpa:8443/redoc"], "Accept-Encoding": ["gzip, deflate, br, zstd"]}, "tls": {"resumed": false, "version": 772, "cipher_suite": 4865, "proto": "h2", "server_name": "api.home.arpa"}}, "bytes_read": 0, "user_id": "", "duration": 0.000964279, "size": 3393, "status": 200, "resp_headers": {"Alt-Svc": ["h3=\":443\"; ma=2592000"], "Date": ["Sat, 25 Oct 2025 01:47:49 GMT"], "Content-Type": ["application/json"], "Content-Length": ["3393"], "Server": ["Caddy", "BaseHTTP/0.6 Python/3.13.7"]}}
[caddy] 2025/10/25 01:47:54.957 INFO    http.log.access.log2    handled request{"request": {"remote_ip": "172.26.0.1", "remote_port": "40850", "client_ip": "172.26.0.1", "proto": "HTTP/2.0", "method": "GET", "host": "docs.home.arpa:8443", "uri": "/concepts.html", "headers": {"Sec-Ch-Ua-Mobile": ["?0"], "Sec-Fetch-Mode": ["navigate"], "Referer": ["https://docs.home.arpa:8443/"], "Accept-Encoding": ["gzip, deflate, br, zstd"], "Accept": ["text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7"], "Sec-Fetch-Site": ["same-origin"], "Sec-Fetch-User": ["?1"], "Sec-Ch-Ua-Platform": ["\"Linux\""], "Sec-Fetch-Dest": ["document"], "Accept-Language": ["en-US,en;q=0.9"], "Priority": ["u=0, i"], "Sec-Ch-Ua": ["\"Chromium\";v=\"140\", \"Not=A?Brand\";v=\"24\", \"Google Chrome\";v=\"140\""], "Upgrade-Insecure-Requests": ["1"], "User-Agent": ["Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"]}, "tls": {"resumed": false, "version": 772, "cipher_suite": 4865, "proto": "h2", "server_name": "docs.home.arpa"}}, "bytes_read": 0, "user_id": "", "duration": 0.001245247, "size": 7023, "status": 200, "resp_headers": {"Content-Length": ["7023"], "Last-Modified": ["Sat, 25 Oct 2025 01:46:35 GMT"], "Server": ["Caddy", "SimpleHTTP/0.6 Python/3.13.7"], "Alt-Svc": ["h3=\":443\"; ma=2592000"], "Date": ["Sat, 25 Oct 2025 01:47:54 GMT"], "Content-Type": ["text/html"]}}
[caddy] 2025/10/25 01:47:59.256 INFO    http.log.access.log2    handled request{"request": {"remote_ip": "172.26.0.1", "remote_port": "40850", "client_ip": "172.26.0.1", "proto": "HTTP/2.0", "method": "GET", "host": "docs.home.arpa:8443", "uri": "/http-api.html", "headers": {"Sec-Fetch-User": ["?1"], "Referer": ["https://docs.home.arpa:8443/concepts.html"], "Priority": ["u=0, i"], "Sec-Fetch-Site": ["same-origin"], "Sec-Ch-Ua-Platform": ["\"Linux\""], "Upgrade-Insecure-Requests": ["1"], "Sec-Fetch-Mode": ["navigate"], "Accept-Encoding": ["gzip, deflate, br, zstd"], "Sec-Ch-Ua": ["\"Chromium\";v=\"140\", \"Not=A?Brand\";v=\"24\", \"Google Chrome\";v=\"140\""], "Accept-Language": ["en-US,en;q=0.9"], "Accept": ["text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7"], "User-Agent": ["Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"], "Sec-Fetch-Dest": ["document"], "Sec-Ch-Ua-Mobile": ["?0"]}, "tls": {"resumed": false, "version": 772, "cipher_suite": 4865, "proto": "h2", "server_name": "docs.home.arpa"}}, "bytes_read": 0, "user_id": "", "duration": 0.00121905, "size": 8410, "status": 200, "resp_headers": {"Server": ["Caddy", "SimpleHTTP/0.6 Python/3.13.7"], "Alt-Svc": ["h3=\":443\"; ma=2592000"], "Date": ["Sat, 25 Oct 2025 01:47:59 GMT"], "Content-Type": ["text/html"], "Content-Length": ["8410"], "Last-Modified": ["Sat, 25 Oct 2025 01:46:35 GMT"]}}
[caddy] 2025/10/25 01:48:01.727 INFO    http.log.access.log2    handled request{"request": {"remote_ip": "172.26.0.1", "remote_port": "40850", "client_ip": "172.26.0.1", "proto": "HTTP/2.0", "method": "GET", "host": "docs.home.arpa:8443", "uri": "/architecture.html", "headers": {"Sec-Fetch-Dest": ["document"], "Sec-Ch-Ua-Mobile": ["?0"], "Sec-Ch-Ua-Platform": ["\"Linux\""], "Upgrade-Insecure-Requests": ["1"], "Sec-Fetch-User": ["?1"], "Referer": ["https://docs.home.arpa:8443/http-api.html"], "Accept": ["text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7"], "Sec-Fetch-Site": ["same-origin"], "Sec-Fetch-Mode": ["navigate"], "Priority": ["u=0, i"], "Sec-Ch-Ua": ["\"Chromium\";v=\"140\", \"Not=A?Brand\";v=\"24\", \"Google Chrome\";v=\"140\""], "User-Agent": ["Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"], "Accept-Encoding": ["gzip, deflate, br, zstd"], "Accept-Language": ["en-US,en;q=0.9"]}, "tls": {"resumed": false, "version": 772, "cipher_suite": 4865, "proto": "h2", "server_name": "docs.home.arpa"}}, "bytes_read": 0, "user_id": "", "duration": 0.00126644, "size": 16924, "status": 200, "resp_headers": {"Content-Length": ["16924"], "Last-Modified": ["Sat, 25 Oct 2025 01:46:35 GMT"], "Server": ["Caddy", "SimpleHTTP/0.6 Python/3.13.7"], "Alt-Svc": ["h3=\":443\"; ma=2592000"], "Date": ["Sat, 25 Oct 2025 01:48:01 GMT"], "Content-Type": ["text/html"]}}
[controller] 2025-10-24 18:48:02 WARNING ae.runtime.docker_runtime: Failed to stop container ae-echo-rev284-0: 404 Client Error for http+docker://localhost/v1.45/containers/4dfab3f74c7e4e736542e863b9d8f703f3e16c7ce45c70af2f6feed326b08505/stop?t=10: Not Found ("No such container: 4dfab3f74c7e4e736542e863b9d8f703f3e16c7ce45c70af2f6feed326b08505")
[controller] 2025-10-24 18:48:02 WARNING ae.runtime.docker_runtime: Failed to remove container ae-echo-rev284-0: 404 Client Error for http+docker://localhost/v1.45/containers/4dfab3f74c7e4e736542e863b9d8f703f3e16c7ce45c70af2f6feed326b08505?v=False&link=False&force=False: Not Found ("No such container: 4dfab3f74c7e4e736542e863b9d8f703f3e16c7ce45c70af2f6feed326b08505")
[controller] 2025-10-24 18:48:02 WARNING ae.runtime.docker_runtime: Failed to stop container ae-echo-rev283-0: 404 Client Error for http+docker://localhost/v1.45/containers/ecf2ff1d55227f33a0a3672ad861e98e39dff90ee871411863c563fb5e25c122/stop?t=10: Not Found ("No such container: ecf2ff1d55227f33a0a3672ad861e98e39dff90ee871411863c563fb5e25c122")
[controller] 2025-10-24 18:48:02 WARNING ae.runtime.docker_runtime: Failed to remove container ae-echo-rev283-0: 404 Client Error for http+docker://localhost/v1.45/containers/ecf2ff1d55227f33a0a3672ad861e98e39dff90ee871411863c563fb5e25c122?v=False&link=False&force=False: Not Found ("No such container: ecf2ff1d55227f33a0a3672ad861e98e39dff90ee871411863c563fb5e25c122")
[controller] 2025-10-24 18:48:12 WARNING ae.runtime.docker_runtime: Failed to remove container ae-echo-rev282-0: 409 Client Error for http+docker://localhost/v1.45/containers/f3dea527af96dde22e03c6957c96cf01588212954c6e3dc93193ac61d78ed724?v=False&link=False&force=False: Conflict ("removal of container f3dea527af96dde22e03c6957c96cf01588212954c6e3dc93193ac61d78ed724 is already in progress")
[controller] Traceback (most recent call last):
[controller]   File "/home/m4xx3d0ut/git/k1s/src/ae/secrets/manager.py", line 71, in _decrypt
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
[controller]   File "/home/m4xx3d0ut/git/k1s/src/ae/secrets/manager.py", line 101, in _decrypt
[controller]     raise RuntimeError(f"sops decrypt failed for {path}: {exc.stderr}") from exc
[controller] RuntimeError: sops decrypt failed for specs/examples/demo-secret.sops.yaml: sops metadata not found
[controller]
[controller] Applied blue: +0/~0/-0, ready=1, live=1, rev=3(ready)
[controller] Applied echo: +1/~0/-0, ready=0, live=1, rev=283(progressing)
[controller] Applied echo-stateful: +0/~0/-0, ready=1, live=1, rev=1(ready)
[controller] Applied echo-del: +0/~0/-0, ready=1, live=1, rev=1(ready)
[controller] Applied echo: +1/~0/-0, ready=1, live=1, rev=285(ready)
[caddy] 2025/10/25 01:48:13.986 INFO    http.log.access.log2    handled request{"request": {"remote_ip": "172.26.0.1", "remote_port": "41986", "client_ip": "172.26.0.1", "proto": "HTTP/2.0", "method": "GET", "host": "docs.home.arpa:8443", "uri": "/overview.html", "headers": {"Sec-Ch-Ua": ["\"Chromium\";v=\"140\", \"Not=A?Brand\";v=\"24\", \"Google Chrome\";v=\"140\""], "Sec-Ch-Ua-Platform": ["\"Linux\""], "Sec-Fetch-Dest": ["document"], "Accept-Encoding": ["gzip, deflate, br, zstd"], "Sec-Ch-Ua-Mobile": ["?0"], "Accept": ["text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7"], "Sec-Fetch-Site": ["same-origin"], "Sec-Fetch-Mode": ["navigate"], "Upgrade-Insecure-Requests": ["1"], "Referer": ["https://docs.home.arpa:8443/architecture.html"], "Accept-Language": ["en-US,en;q=0.9"], "Priority": ["u=0, i"], "User-Agent": ["Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"], "Sec-Fetch-User": ["?1"]}, "tls": {"resumed": false, "version": 772, "cipher_suite": 4865, "proto": "h2", "server_name": "docs.home.arpa"}}, "bytes_read": 0, "user_id": "", "duration": 0.0011307, "size": 8040, "status": 200, "resp_headers": {"Content-Length": ["8040"], "Server": ["Caddy", "SimpleHTTP/0.6 Python/3.13.7"], "Alt-Svc": ["h3=\":443\"; ma=2592000"], "Last-Modified": ["Sat, 25 Oct 2025 01:46:35 GMT"], "Date": ["Sat, 25 Oct 2025 01:48:13 GMT"], "Content-Type": ["text/html"]}}
[caddy] 2025/10/25 01:48:24.968 INFO    http.log.access.log2    handled request{"request": {"remote_ip": "172.26.0.1", "remote_port": "45970", "client_ip": "172.26.0.1", "proto": "HTTP/2.0", "method": "GET", "host": "docs.home.arpa:8443", "uri": "/index.html", "headers": {"Sec-Ch-Ua": ["\"Chromium\";v=\"140\", \"Not=A?Brand\";v=\"24\", \"Google Chrome\";v=\"140\""], "Upgrade-Insecure-Requests": ["1"], "User-Agent": ["Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"], "Sec-Fetch-Site": ["same-origin"], "Accept-Encoding": ["gzip, deflate, br, zstd"], "Sec-Ch-Ua-Mobile": ["?0"], "Sec-Fetch-Mode": ["navigate"], "Sec-Fetch-User": ["?1"], "Sec-Fetch-Dest": ["document"], "Accept-Language": ["en-US,en;q=0.9"], "Accept": ["text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7"], "Priority": ["u=0, i"], "Sec-Ch-Ua-Platform": ["\"Linux\""], "Referer": ["https://docs.home.arpa:8443/overview.html"]}, "tls": {"resumed": false, "version": 772, "cipher_suite": 4865, "proto": "h2", "server_name": "docs.home.arpa"}}, "bytes_read": 0, "user_id": "", "duration": 0.001127067, "size": 4424, "status": 200, "resp_headers": {"Last-Modified": ["Sat, 25 Oct 2025 01:46:35 GMT"], "Server": ["Caddy", "SimpleHTTP/0.6 Python/3.13.7"], "Alt-Svc": ["h3=\":443\"; ma=2592000"], "Date": ["Sat, 25 Oct 2025 01:48:24 GMT"], "Content-Type": ["text/html"], "Content-Length": ["4424"]}}
[caddy] 2025/10/25 01:48:38.722 INFO    http.log.access.log1    handled request{"request": {"remote_ip": "172.26.0.1", "remote_port": "55940", "client_ip": "172.26.0.1", "proto": "HTTP/2.0", "method": "GET", "host": "blue.home.arpa:8443", "uri": "/", "headers": {"User-Agent": ["curl/8.15.0"], "Accept": ["*/*"]}, "tls": {"resumed": false, "version": 772, "cipher_suite": 4865, "proto": "h2", "server_name": "blue.home.arpa"}}, "bytes_read": 0, "user_id": "", "duration": 0.0011887, "size": 23, "status": 200, "resp_headers": {"Content-Length": ["23"], "Server": ["Caddy", "BaseHTTP/0.6 Python/3.12.12"], "Alt-Svc": ["h3=\":443\"; ma=2592000"], "Date": ["Sat, 25 Oct 2025 01:48:38 GMT"], "Content-Type": ["text/plain; charset=utf-8"]}}
[caddy] 2025/10/25 01:48:43.117 INFO    http.log.access.log4    handled request{"request": {"remote_ip": "172.26.0.1", "remote_port": "60798", "client_ip": "172.26.0.1", "proto": "HTTP/2.0", "method": "GET", "host": "green.home.arpa:8443", "uri": "/", "headers": {"User-Agent": ["curl/8.15.0"], "Accept": ["*/*"]}, "tls": {"resumed": false, "version": 772, "cipher_suite": 4865, "proto": "h2", "server_name": "green.home.arpa"}}, "bytes_read": 0, "user_id": "", "duration": 0.001658272, "size": 25, "status": 200, "resp_headers": {"Content-Length": ["25"], "Server": ["Caddy", "BaseHTTP/0.6 Python/3.12.12"], "Alt-Svc": ["h3=\":443\"; ma=2592000"], "Date": ["Sat, 25 Oct 2025 01:48:43 GMT"], "Content-Type": ["text/plain; charset=utf-8"]}}
[controller] 2025-10-24 18:48:45 INFO __main__: http api listening on port 9108
[controller] 2025-10-24 18:48:45 INFO __main__: watchdog not available; falling back to interval polling
[sites] https://blue.home.arpa {
[sites]     log {
[sites]         output stdout
[sites]         format console
[sites]     }
[sites]     # Ensure upstream HSTS does not stick during dev
[sites]     header -Strict-Transport-Security
[sites]     reverse_proxy host.docker.internal:33353 {
[sites]
[sites]         lb_policy first
[sites]     }
[sites] }
[caddy] {"level":"info","ts":1761356926.118515,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"59718","headers":{"Accept-Encoding":["gzip"],"Content-Length":["2621"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
[caddy] {"level":"info","ts":1761356926.1186814,"msg":"config is unchanged"}
[caddy] {"level":"info","ts":1761356926.1186883,"logger":"admin.api","msg":"load complete"}
[controller] Traceback (most recent call last):
[controller]   File "/home/m4xx3d0ut/git/k1s/.venv-demo/lib/python3.13/site-packages/docker/api/client.py", line 275, in _raise_for_status
[controller]     response.raise_for_status()
[controller]     ~~~~~~~~~~~~~~~~~~~~~~~~~^^
[controller]   File "/home/m4xx3d0ut/git/k1s/.venv-demo/lib/python3.13/site-packages/requests/models.py", line 1026, in raise_for_status
[controller]     raise HTTPError(http_error_msg, response=self)
[controller] requests.exceptions.HTTPError: 409 Client Error: Conflict for url: http+docker://localhost/v1.45/containers/create?name=ae-echo-rev293-0
[controller]
[controller] The above exception was the direct cause of the following exception:
[controller]
[controller] Traceback (most recent call last):
[controller]   File "/home/m4xx3d0ut/git/k1s/src/ae/runtime/docker_runtime.py", line 262, in _create_container
[controller]     container = run_fn(
[controller]         manifest.spec.image,
[controller]         **{k: v for k, v in kwargs.items() if v is not None}
[controller]     )
[controller]   File "/home/m4xx3d0ut/git/k1s/.venv-demo/lib/python3.13/site-packages/docker/models/containers.py", line 876, in run
[controller]     container = self.create(image=image, command=command,
[controller]                             detach=detach, **kwargs)
[controller]   File "/home/m4xx3d0ut/git/k1s/.venv-demo/lib/python3.13/site-packages/docker/models/containers.py", line 935, in create
[controller]     resp = self.client.api.create_container(**create_kwargs)
[controller]   File "/home/m4xx3d0ut/git/k1s/.venv-demo/lib/python3.13/site-packages/docker/api/container.py", line 440, in create_container
[controller]     return self.create_container_from_config(config, name, platform)
[controller]            ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~^^^^^^^^^^^^^^^^^^^^^^^^
[controller]   File "/home/m4xx3d0ut/git/k1s/.venv-demo/lib/python3.13/site-packages/docker/api/container.py", line 457, in create_container_from_config
[controller]     return self._result(res, True)
[controller]            ~~~~~~~~~~~~^^^^^^^^^^^
[controller]   File "/home/m4xx3d0ut/git/k1s/.venv-demo/lib/python3.13/site-packages/docker/api/client.py", line 281, in _result
[controller]     self._raise_for_status(response)
[controller]     ~~~~~~~~~~~~~~~~~~~~~~^^^^^^^^^^
[controller]   File "/home/m4xx3d0ut/git/k1s/.venv-demo/lib/python3.13/site-packages/docker/api/client.py", line 277, in _raise_for_status
[controller]     raise create_api_error_from_http_exception(e) from e
[controller]           ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~^^^
[controller]   File "/home/m4xx3d0ut/git/k1s/.venv-demo/lib/python3.13/site-packages/docker/errors.py", line 39, in create_api_error_from_http_exception
[controller]     raise cls(e, response=response, explanation=explanation) from e
[controller] docker.errors.APIError: 409 Client Error for http+docker://localhost/v1.45/containers/create?name=ae-echo-rev293-0: Conflict ("Conflict. The container name "/ae-echo-rev293-0" is already in use by container "6a7d92761686f68e12a9ddf7d76239de9b71908ebc33b6e66d5c2eca62f36276". You have to remove (or rename) that container to be able to reuse that name.")
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
[controller]   File "/home/m4xx3d0ut/git/k1s/src/ae/controller/reconciler.py", line 96, in reconcile
[controller]     result = self._runtime.ensure_app(  # type: ignore[arg-type]
[controller]         manifest_for_runtime, revision, keep_old=True, limit_create=limit_create
[controller]     )
[controller]   File "/home/m4xx3d0ut/git/k1s/src/ae/runtime/docker_runtime.py", line 82, in ensure_app
[controller]     container = self._create_container(manifest, replica_id, revision)
[controller]   File "/home/m4xx3d0ut/git/k1s/src/ae/runtime/docker_runtime.py", line 281, in _create_container
[controller]     raise RuntimeError(f"Failed to create container {name}: {exc}") from exc
[controller] RuntimeError: Failed to create container ae-echo-rev293-0: 409 Client Error for http+docker://localhost/v1.45/containers/create?name=ae-echo-rev293-0: Conflict ("Conflict. The container name "/ae-echo-rev293-0" is already in use by container "6a7d92761686f68e12a9ddf7d76239de9b71908ebc33b6e66d5c2eca62f36276". You have to remove (or rename) that container to be able to reuse that name.")
[controller] Applied blue: +0/~0/-0, ready=1, live=1, rev=3(ready)
[controller] Applied echo: +0/~0/-0, ready=0, live=1, rev=292(progressing)
[controller] Applied echo-stateful: +0/~0/-0, ready=1, live=1, rev=1(ready)
[controller] Applied echo-del: +0/~0/-0, ready=1, live=1, rev=1(ready)

