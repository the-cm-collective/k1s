# VM Golden Image Pipeline (Ubuntu 22.04 GA)

Purpose
- Build reproducible golden VM images for k1s fabric testing.
- Keep Ubuntu on GA kernel `5.15` to avoid HWE/vGPU drift.

Inputs
- Packer template: `lab/packer/ubuntu-22.04-ga.pkr.hcl`
- Build scripts: `scripts/lab/vm/labctl.sh image ...`
- Image lock: `lab/images/images.lock.yaml`

## 1) Build images

```bash
scripts/lab/vm/labctl.sh image build --variant all
```

Outputs
- `artifacts/images/ubuntu-22.04-k1s-base.qcow2`
- `artifacts/images/ubuntu-22.04-k1s-gpu.qcow2`
- `*.sha256` and `*.meta.json`

## 2) Verify images

```bash
scripts/lab/vm/labctl.sh image verify --variant all
```

Checks
- checksum validation
- qcow2 format validation (`qemu-img info`)
- metadata kernel track `ga-5.15`
- metadata readiness flags:
  - `vm_bootstrap_ready`
  - `python_alias`
  - `crictl_ready`
  - `cni_ready`

The VM smoke and drill lanes now treat those readiness flags as authoritative. If
`image verify` fails, rebuild the images before running `make lab-vm-smoke`.

## 3) Transfer images to test host

```bash
scripts/lab/vm/labctl.sh image transfer \
  --host user@host-a \
  --dest /var/lib/k1s/images \
  --variant all
```

Remote verification is executed immediately after transfer.

## 4) Pin active image set

Use `lab/images/images.lock.yaml` as the source of truth for active base/gpu image paths in variants.

## Notes
- GPU image installs NVIDIA driver/toolkit packages, but runtime readiness still requires host GPU passthrough/vGPU assignment.
- Do not switch to Ubuntu HWE kernel for this lane unless you explicitly run a separate compatibility matrix.
- The image is responsible for the guest bootstrap prerequisites used by the VM smoke/drill lanes:
  - `python-is-python3`
  - `crictl`
  - `/etc/crictl.yaml`
  - populated `/opt/cni/bin`
  - default bridge + loopback CNI config
- Guest-side bootstrap repair is no longer the default contract. `AE_VM_BOOTSTRAP_AUTOFIX=1` remains available only as a manual debug escape hatch.
