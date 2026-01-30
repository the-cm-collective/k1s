# K1S Service User (k1s)

This document standardizes how we create and manage the `k1s` service user for both development and deployment. It covers home directory selection, required permissions, runtime defaults (Podman), optional Docker support, and how we handle certificate trust.

## Goals
- Provide a predictable, least‑privilege user for running demos and local reconciliation loops.
- Default to rootless Podman; allow optional Docker without sudo.
- Make the setup idempotent and portable across Debian/Ubuntu and RHEL/Fedora families.
- Keep secrets, registries, logs, and state under the `k1s` home.

## Modes
- Development: `HOME` is the repo path, e.g., `/home/<you>/git/k1s`.
- Deployment: `HOME` is `/opt/k1s`.

## Summary: Required/Optional Groups
- Required (all installs)
  - `k1s` (primary group): own runtime dirs and artifacts.
  - `systemd-journal`: read journald for `ae cli events`/metrics without full `/var/log` access.
- Conditional (Podman rootless)
  - `fuse`: only if `/dev/fuse` is restricted on the host; many distros expose it `0666` and don’t require group membership. Safe to include when present.
- Optional (host log files)
  - `adm` (Debian/Ubuntu): read some files under `/var/log`. Not needed if journald is sufficient.
- Optional (Docker runtime)
  - `docker`: grants access to `/var/run/docker.sock` so Docker can be used without sudo.
- Optional (CRI/containerd runtime)
  - Access to `/run/containerd/containerd.sock` (typically root‑owned); run as root or grant socket access via group/ACL if you want CRI without sudo.
  - Dev helper: `scripts/containerd_socket_access.sh --grant` (restores with `--revoke`).
- Not required
  - No special `podman` group is needed; Podman rootless works per‑user.
  - `netdev` is not required for rootless networking (handled by `slirp4netns`).

## Certificate Trust Strategy
We prioritize non‑root, user‑scoped trust for registries and services the k1s toolchain talks to.

- Containers/Registries (rootless, preferred)
  - Place CA certs under: `~/.config/containers/certs.d/<registry>/ca.crt` (Podman) so pulls/pushes trust the registry.
- System CA bundle (only when required)
  - Updating the system trust store requires root and is not gated by a group. Options:
    1) Sudo membership: add `k1s` to `sudo`/`wheel` (broad). Not recommended by default.
    2) Polkit/sudoers narrow allowlist: permit only `update-ca-certificates` (Debian/Ubuntu) or `update-ca-trust` (RHEL/Fedora) via a wrapper script.
  - Recommendation: Use user‑scoped container trust wherever possible; reserve system‑wide trust updates for deployment images or controlled ops.

## Podman (Default Runtime)
- Packages: `podman`, `uidmap`, `slirp4netns`, `fuse-overlayfs`.
- Rootless requirements:
  - Ensure `newuidmap/newgidmap` are present (`uidmap` package) and that `/etc/subuid` and `/etc/subgid` include an allocation for `k1s` (e.g., `k1s:100000:65536`). Most distros allocate automatically; verify and add if missing.
  - Optional: `loginctl enable-linger k1s` to allow user services to run without an active login session.

## Docker (Optional Runtime)
- Add `k1s` to the `docker` group to access the daemon socket without sudo.
- Keep Docker optional; do not grant `sudo` for Docker commands.

## Directories and Ownership
Create and own these paths as `k1s:k1s`:
- `<HOME>/state` – SQLite DB and runtime artifacts.
- `<HOME>/logs` – controller, ingress, runtime logs (if not using journald export).
- `<HOME>/.config/ae` – registries and keys;
  - `<HOME>/.config/ae/registries.yaml` (0600)
  - `<HOME>/.config/ae/keys.txt` (0600)
- Deployment only: `/opt/k1s` (0750) with subdirs as above.

## Implementation Options
We standardize on a portable script for contributors and systemd primitives for deployments.

1) Portable script (dev and quick starts)
- Path: `scripts/setup-k1s-user.sh` with `--mode dev|deploy` or `--home <path>`
- Behavior:
  - Create or update `k1s` user; set shell to `/bin/bash` (dev) or `/usr/sbin/nologin` (deploy) if only used by services.
  - Set home to repo path for `--mode dev`, `/opt/k1s` for `--mode deploy` (create if absent).
  - Ensure group memberships: `k1s`, `systemd-journal`, `fuse` (if present), `docker` (when `--with-docker`).
  - Verify/allocate subuid/subgid ranges for rootless Podman.
  - Create runtime directories and fix ownership/permissions (idempotent).
  - Optional: enable user linger for systemd units in deploy mode.

2) systemd‑sysusers + tmpfiles (deploy)
- Files to ship under `ops/` and apply during install.

Example `ops/sysusers.d/k1s.conf`:
```
# Type  Name  ID  GECOS               Home
g k1s
u k1s   -   "K1S Service User"     /opt/k1s
m k1s systemd-journal
m k1s fuse
# Add docker membership only when Docker is intentionally enabled
# m k1s docker
```

Example `ops/tmpfiles.d/k1s.conf`:
```
# Path                 Mode  User Group  Age  Argument
d /opt/k1s             0750  k1s  k1s    -    -
d /opt/k1s/state       0750  k1s  k1s    -    -
d /opt/k1s/logs        0750  k1s  k1s    -    -
d /opt/k1s/.config     0700  k1s  k1s    -    -
d /opt/k1s/.config/ae  0700  k1s  k1s    -    -
f /opt/k1s/.config/ae/registries.yaml 0600 k1s k1s - -
f /opt/k1s/.config/ae/keys.txt        0600 k1s k1s - -
```

Apply during install:
- `systemd-sysusers --replace=/path/to/ops/sysusers.d/k1s.conf`
- `systemd-tmpfiles --create /path/to/ops/tmpfiles.d/k1s.conf`

## Sudoers/Polkit (for system CA updates only)
If you must update the system CA store from automation, prefer a narrow allowlist.

- Debian/Ubuntu sudoers example (edit via `visudo`):
```
Defaults!update-ca-certificates !requiretty
k1s ALL=(root) NOPASSWD: /usr/sbin/update-ca-certificates
```
- RHEL/Fedora sudoers example:
```
Defaults!update-ca-trust !requiretty
k1s ALL=(root) NOPASSWD: /usr/bin/update-ca-trust
```
- Polkit rule example (RHEL/Fedora‑style, place under `/etc/polkit-1/rules.d/50-k1s-ca.rules`):
```
polkit.addRule(function(action, subject) {
  if ((action.id == "org.k1s.update-trust") && subject.user == "k1s") {
    return polkit.Result.YES;
  }
});
```
Pair the rule with a small root‑owned helper that performs only the trust update.

## Validation Checklist
- User exists with correct home: `getent passwd k1s` → home matches mode.
- Groups: `id k1s` includes `systemd-journal` (and `docker` if enabled).
- Rootless Podman works: `sudo -u k1s podman info --log-level=error`.
- Subuid/subgid allocated: `grep ^k1s: /etc/subuid /etc/subgid`.
- Optional linger: `loginctl show-user k1s | grep Linger=yes`.

## Notes and Rationale
- We prefer journald access via `systemd-journal` over broad `/var/log` access.
- We avoid blanket sudo by default; Docker access is gated by membership in `docker`.
- For Podman, the `fuse` group is a safe compatibility addition across hosts that restrict `/dev/fuse`.
- Certificate trust is user‑scoped unless there is a clear operational need for system‑wide updates.
