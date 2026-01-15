# Install the AE Controller with systemd

This installs the controller as a simple service on a Linux host (Debian/Ubuntu/RHEL).

Prereqs
- Python 3.11+
- Podman (recommended) or Docker

Steps
1) Create user and directories:
```
sudo useradd -r -s /bin/false ae || true
sudo mkdir -p /var/lib/ae /etc/ae/specs
sudo chown -R ae:ae /var/lib/ae /etc/ae
```

2) Install the package (venv recommended):
```
python -m pip install --upgrade pip
python -m pip install k1s  # or pip install -e . in this repo
```

3) Install the service unit:
```
sudo install -m 0644 ops/systemd/ae-controller.service /etc/systemd/system/
```

4) Start and enable:
```
sudo systemctl daemon-reload
sudo systemctl enable --now ae-controller
```

5) Place manifests under `/etc/ae/specs` and watch the dashboard at `http://<host>:9108`.

Notes
- Customize AE_STATE_DB and AE_SPECS_DIR via `Environment=` in the unit.
- To use Docker, set `AE_RUNTIME_BACKEND=docker` in the unit `Environment=`.
- Logs are visible via `journalctl -u ae-controller`.


Switching to Docker backend (drop-in)
```
sudo mkdir -p /etc/systemd/system/ae-controller.service.d
sudo install -m 0644 ops/systemd/ae-controller.d/override-docker.conf \
  /etc/systemd/system/ae-controller.service.d/override.conf
sudo systemctl daemon-reload && sudo systemctl restart ae-controller
```
