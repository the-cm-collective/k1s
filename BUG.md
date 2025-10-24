2025/10/24 02:03:13.971	[34mINFO[0m	http.log.access.log0	handled request	{"request": {"remote_ip": "172.26.0.1", "remote_port": "43210", "client_ip": "172.26.0.1", "proto": "HTTP/1.1", "method": "GET", "host": "blue.home.arpa:8080", "uri": "/", "headers": {"User-Agent": ["curl/8.15.0"], "Accept": ["*/*"]}}, "bytes_read": 0, "user_id": "", "duration": 0.001228303, "size": 23, "status": 200, "resp_headers": {"Server": ["Caddy", "BaseHTTP/0.6 Python/3.12.12"], "Date": ["Fri, 24 Oct 2025 02:03:13 GMT"], "Content-Type": ["text/plain; charset=utf-8"], "Content-Length": ["23"]}}
2025/10/24 02:03:18.946	[34mINFO[0m	http.log.access.log1	handled request	{"request": {"remote_ip": "172.26.0.1", "remote_port": "48352", "client_ip": "172.26.0.1", "proto": "HTTP/1.1", "method": "GET", "host": "docs.home.arpa:8080", "uri": "/", "headers": {"Accept": ["*/*"], "User-Agent": ["curl/8.15.0"]}}, "bytes_read": 0, "user_id": "", "duration": 0.003095539, "size": 1216, "status": 200, "resp_headers": {"Server": ["Caddy", "SimpleHTTP/0.6 Python/3.13.7"], "Date": ["Fri, 24 Oct 2025 02:03:18 GMT"], "Content-Type": ["text/html"], "Content-Length": ["1216"], "Last-Modified": ["Fri, 24 Oct 2025 02:02:27 GMT"]}}
2025/10/24 02:03:44.267	[34mINFO[0m	http.log.access.log1	handled request	{"request": {"remote_ip": "172.26.0.1", "remote_port": "48628", "client_ip": "172.26.0.1", "proto": "HTTP/1.1", "method": "GET", "host": "docs.home.arpa:8080", "uri": "/", "headers": {"User-Agent": ["curl/8.15.0"], "Accept": ["*/*"]}}, "bytes_read": 0, "user_id": "", "duration": 0.00111884, "size": 1216, "status": 200, "resp_headers": {"Last-Modified": ["Fri, 24 Oct 2025 02:02:27 GMT"], "Server": ["Caddy", "SimpleHTTP/0.6 Python/3.13.7"], "Date": ["Fri, 24 Oct 2025 02:03:44 GMT"], "Content-Type": ["text/html"], "Content-Length": ["1216"]}}






caddy-1       | {"level":"info","ts":1761271344.4028282,"msg":"using config from file","file":"/etc/caddy/Caddyfile"}
caddy-1       | {"level":"info","ts":1761271344.4040694,"msg":"adapted config to JSON","adapter":"caddyfile"}
caddy-1       | {"level":"warn","ts":1761271344.404077,"msg":"Caddyfile input is not formatted; run 'caddy fmt --overwrite' to fix inconsistencies","adapter":"caddyfile","file":"/etc/caddy/Caddyfile","line":3}
caddy-1       | {"level":"info","ts":1761271344.4048693,"logger":"admin","msg":"admin endpoint started","address":"localhost:2019","enforce_origin":false,"origins":["//localhost:2019","//[::1]:2019","//127.0.0.1:2019"]}
prometheus-1  | ts=2025-10-24T02:02:24.383Z caller=main.go:589 level=info msg="No time or size retention was set so using the default time retention" duration=15d
caddy-1       | {"level":"warn","ts":1761271344.4050045,"logger":"http.auto_https","msg":"server is listening only on the HTTP port, so no automatic HTTPS will be applied to this server","server_name":"srv0","http_port":80}
caddy-1       | {"level":"info","ts":1761271344.4050858,"logger":"tls.cache.maintenance","msg":"started background certificate maintenance","cache":"0xc00061bd00"}
caddy-1       | {"level":"info","ts":1761271344.4052598,"logger":"http.log","msg":"server running","name":"srv0","protocols":["h1","h2","h3"]}
caddy-1       | {"level":"info","ts":1761271344.4115746,"msg":"autosaved config (load with --resume flag)","file":"/config/caddy/autosave.json"}
caddy-1       | {"level":"info","ts":1761271344.411592,"msg":"serving initial configuration"}
prometheus-1  | ts=2025-10-24T02:02:24.383Z caller=main.go:633 level=info msg="Starting Prometheus Server" mode=server version="(version=2.53.0, branch=HEAD, revision=4c35b9250afefede41c5f5acd76191f90f625898)"
prometheus-1  | ts=2025-10-24T02:02:24.383Z caller=main.go:638 level=info build_context="(go=go1.22.4, platform=linux/amd64, user=root@7f8d89cbbd64, date=20240619-07:39:12, tags=netgo,builtinassets,stringlabels)"
prometheus-1  | ts=2025-10-24T02:02:24.383Z caller=main.go:639 level=info host_details="(Linux 6.12.38+kali-amd64 #1 SMP PREEMPT_DYNAMIC Kali 6.12.38-1kali1 (2025-08-12) x86_64 6bc5e38f4180 (none))"
prometheus-1  | ts=2025-10-24T02:02:24.383Z caller=main.go:640 level=info fd_limits="(soft=524288, hard=524288)"
prometheus-1  | ts=2025-10-24T02:02:24.383Z caller=main.go:641 level=info vm_limits="(soft=unlimited, hard=unlimited)"
prometheus-1  | ts=2025-10-24T02:02:24.386Z caller=web.go:568 level=info component=web msg="Start listening for connections" address=0.0.0.0:9090
prometheus-1  | ts=2025-10-24T02:02:24.387Z caller=main.go:1148 level=info msg="Starting TSDB ..."
prometheus-1  | ts=2025-10-24T02:02:24.389Z caller=tls_config.go:313 level=info component=web msg="Listening on" address=[::]:9090
prometheus-1  | ts=2025-10-24T02:02:24.389Z caller=tls_config.go:316 level=info component=web msg="TLS is disabled." http2=false address=[::]:9090
prometheus-1  | ts=2025-10-24T02:02:24.392Z caller=head.go:626 level=info component=tsdb msg="Replaying on-disk memory mappable chunks if any"
prometheus-1  | ts=2025-10-24T02:02:24.393Z caller=head.go:713 level=info component=tsdb msg="On-disk memory mappable chunks replay completed" duration=2.65µs
prometheus-1  | ts=2025-10-24T02:02:24.393Z caller=head.go:721 level=info component=tsdb msg="Replaying WAL, this may take a while"
prometheus-1  | ts=2025-10-24T02:02:24.393Z caller=head.go:793 level=info component=tsdb msg="WAL segment loaded" segment=0 maxSegment=0
prometheus-1  | ts=2025-10-24T02:02:24.393Z caller=head.go:830 level=info component=tsdb msg="WAL replay completed" checkpoint_replay_duration=51.396µs wal_replay_duration=483.833µs wbl_replay_duration=248ns chunk_snapshot_load_duration=0s mmap_chunk_replay_duration=2.65µs total_replay_duration=570.846µs
prometheus-1  | ts=2025-10-24T02:02:24.395Z caller=main.go:1169 level=info fs_type=EXT4_SUPER_MAGIC
prometheus-1  | ts=2025-10-24T02:02:24.395Z caller=main.go:1172 level=info msg="TSDB started"
prometheus-1  | ts=2025-10-24T02:02:24.395Z caller=main.go:1354 level=info msg="Loading configuration file" filename=/etc/prometheus/prometheus.yml
caddy-1       | {"level":"info","ts":1761271344.4172564,"logger":"tls","msg":"cleaning storage unit","storage":"FileStorage:/data/caddy"}
caddy-1       | {"level":"info","ts":1761271344.4174879,"logger":"tls","msg":"finished cleaning storage units"}
caddy-1       | {"level":"info","ts":1761271346.6104918,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"59824","headers":{"Accept-Encoding":["gzip"],"Content-Length":["1226"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
caddy-1       | {"level":"info","ts":1761271346.6113536,"logger":"admin","msg":"admin endpoint started","address":"localhost:2019","enforce_origin":false,"origins":["//localhost:2019","//[::1]:2019","//127.0.0.1:2019"]}
prometheus-1  | ts=2025-10-24T02:02:24.396Z caller=main.go:1391 level=info msg="updated GOGC" old=100 new=75
prometheus-1  | ts=2025-10-24T02:02:24.396Z caller=main.go:1402 level=info msg="Completed loading of configuration file" filename=/etc/prometheus/prometheus.yml totalDuration=630.738µs db_storage=1.607µs remote_storage=2.309µs web_handler=688ns query_engine=1.179µs scrape=284.403µs scrape_sd=30.601µs notify=1.142µs notify_sd=1.257µs rules=2.1µs tracing=9.392µs
prometheus-1  | ts=2025-10-24T02:02:24.396Z caller=main.go:1133 level=info msg="Server is ready to receive web requests."
prometheus-1  | ts=2025-10-24T02:02:24.396Z caller=manager.go:164 level=info component="rule manager" msg="Starting rule manager..."
caddy-1       | {"level":"warn","ts":1761271346.6114938,"logger":"http.auto_https","msg":"server is listening only on the HTTP port, so no automatic HTTPS will be applied to this server","server_name":"srv0","http_port":80}
caddy-1       | {"level":"info","ts":1761271346.6117754,"logger":"http.log","msg":"server running","name":"srv0","protocols":["h1","h2","h3"]}
caddy-1       | {"level":"info","ts":1761271346.61179,"logger":"http","msg":"servers shutting down with eternal grace period"}
caddy-1       | {"level":"info","ts":1761271346.612051,"msg":"autosaved config (load with --resume flag)","file":"/config/caddy/autosave.json"}
caddy-1       | {"level":"info","ts":1761271346.6120713,"logger":"admin.api","msg":"load complete"}
caddy-1       | {"level":"info","ts":1761271346.6129463,"logger":"admin","msg":"stopped previous server","address":"localhost:2019"}
caddy-1       | {"level":"info","ts":1761271347.418032,"logger":"admin.api","msg":"received request","method":"POST","host":"localhost:2019","uri":"/load","remote_ip":"127.0.0.1","remote_port":"59826","headers":{"Accept-Encoding":["gzip"],"Content-Length":["1226"],"Content-Type":["application/json"],"Origin":["http://localhost:2019"],"User-Agent":["Go-http-client/1.1"]}}
caddy-1       | {"level":"info","ts":1761271347.4189036,"logger":"admin","msg":"admin endpoint started","address":"localhost:2019","enforce_origin":false,"origins":["//localhost:2019","//[::1]:2019","//127.0.0.1:2019"]}
caddy-1       | {"level":"warn","ts":1761271347.41905,"logger":"http.auto_https","msg":"server is listening only on the HTTP port, so no automatic HTTPS will be applied to this server","server_name":"srv0","http_port":80}
caddy-1       | {"level":"info","ts":1761271347.4193373,"logger":"http.log","msg":"server running","name":"srv0","protocols":["h1","h2","h3"]}
caddy-1       | {"level":"info","ts":1761271347.41936,"logger":"http","msg":"servers shutting down with eternal grace period"}
caddy-1       | {"level":"info","ts":1761271347.4195757,"msg":"autosaved config (load with --resume flag)","file":"/config/caddy/autosave.json"}
caddy-1       | {"level":"info","ts":1761271347.4195895,"logger":"admin.api","msg":"load complete"}
caddy-1       | {"level":"info","ts":1761271347.420187,"logger":"admin","msg":"stopped previous server","address":"localhost:2019"}
caddy-1       | 2025/10/24 02:03:13.971	[34mINFO[0m	http.log.access.log0	handled request	{"request": {"remote_ip": "172.26.0.1", "remote_port": "43210", "client_ip": "172.26.0.1", "proto": "HTTP/1.1", "method": "GET", "host": "blue.home.arpa:8080", "uri": "/", "headers": {"User-Agent": ["curl/8.15.0"], "Accept": ["*/*"]}}, "bytes_read": 0, "user_id": "", "duration": 0.001228303, "size": 23, "status": 200, "resp_headers": {"Server": ["Caddy", "BaseHTTP/0.6 Python/3.12.12"], "Date": ["Fri, 24 Oct 2025 02:03:13 GMT"], "Content-Type": ["text/plain; charset=utf-8"], "Content-Length": ["23"]}}
caddy-1       | 2025/10/24 02:03:18.946	[34mINFO[0m	http.log.access.log1	handled request	{"request": {"remote_ip": "172.26.0.1", "remote_port": "48352", "client_ip": "172.26.0.1", "proto": "HTTP/1.1", "method": "GET", "host": "docs.home.arpa:8080", "uri": "/", "headers": {"Accept": ["*/*"], "User-Agent": ["curl/8.15.0"]}}, "bytes_read": 0, "user_id": "", "duration": 0.003095539, "size": 1216, "status": 200, "resp_headers": {"Server": ["Caddy", "SimpleHTTP/0.6 Python/3.13.7"], "Date": ["Fri, 24 Oct 2025 02:03:18 GMT"], "Content-Type": ["text/html"], "Content-Length": ["1216"], "Last-Modified": ["Fri, 24 Oct 2025 02:02:27 GMT"]}}
caddy-1       | 2025/10/24 02:03:44.267	[34mINFO[0m	http.log.access.log1	handled request	{"request": {"remote_ip": "172.26.0.1", "remote_port": "48628", "client_ip": "172.26.0.1", "proto": "HTTP/1.1", "method": "GET", "host": "docs.home.arpa:8080", "uri": "/", "headers": {"User-Agent": ["curl/8.15.0"], "Accept": ["*/*"]}}, "bytes_read": 0, "user_id": "", "duration": 0.00111884, "size": 1216, "status": 200, "resp_headers": {"Last-Modified": ["Fri, 24 Oct 2025 02:02:27 GMT"], "Server": ["Caddy", "SimpleHTTP/0.6 Python/3.13.7"], "Date": ["Fri, 24 Oct 2025 02:03:44 GMT"], "Content-Type": ["text/html"], "Content-Length": ["1216"]}}
