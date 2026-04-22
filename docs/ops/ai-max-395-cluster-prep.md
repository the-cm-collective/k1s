# AI Max+ 395 Cluster Prep

Status: actionable hardware-prep and bring-up checklist for the first AI Max+ 395 fabric cell.

This page turns the hardware baseline into a repeatable prep sequence for one 4-node AI Max+ 395 cell. It supports D0 execution-baseline work today and F4 acceleration work later. The canonical hardware choices are documented in [AI Max+ 395 Hardware Baseline](ai-max-395-hardware-baseline.html).

## Scope

This guide covers:

- node assembly and mechanical checks
- baseline storage and network role assignment
- Linux package and driver prep
- direct-connect RoCE validation
- switched RoCE follow-up
- evidence capture for roadmap work

This guide does not claim that RoCE is required for D0. Standard Ethernet remains the correctness baseline.

## Per-Node Checklist

Before first power-on, confirm each node has:

- the AI Max+ 395 platform in its intended enclosure or open test fixture
- the canonical SSD class installed if the cell is using one SSD type cluster-wide
- onboard 5GbE reserved for management
- the E810 installed only on nodes participating in the RoCE development path
- adequate airflow across both the platform and the E810 heatsink
- a DAC cable path available for the first point-to-point validation lane

Capture for each node:

- node identifier
- SSD model and firmware
- NIC presence and BDF
- enclosure or test-fixture notes
- any thermal or clearance caveats

## Interface Role Plan

Use this role map for early bring-up:

| Interface | Role | Phase |
| --- | --- | --- |
| onboard 5GbE | management, provisioning, fallback SSH/API | D0 and later |
| E810 port 1 | primary fabric / RoCE development path | F4 prep begins here |
| E810 port 2 | disabled initially; later storage, uplink, or second traffic domain | only after port 1 is stable |

Do not mix management and RDMA traffic during initial bring-up if it can be avoided.

## Linux Package Baseline

The package set should cover these tools, using distro-appropriate names:

- `pciutils`
- `ethtool`
- `iproute2`
- `rdma-core`
- `lldpad`
- `perftest`
- `infiniband-diags`

The first software sequence is:

1. verify the NIC enumerates on PCIe
2. bring up the `ice` driver
3. verify the Ethernet link path is healthy
4. load `irdma`
5. verify verbs and RDMA device visibility

## Preflight Commands

Run these before any switched-fabric work:

```bash
lspci -nn | grep -i ethernet
lspci -vv -s <E810_BDF>
ethtool -i <fabric_if>
ip link show <fabric_if>
sudo modprobe ice
sudo modprobe irdma
lsmod | grep -E 'ice|irdma'
rdma link
ibv_devinfo
```

Record:

- negotiated PCIe width and speed
- driver names and versions
- RDMA device visibility
- link state and media type

## Phase 0: Standard Ethernet Baseline

Before enabling any RoCE assumptions, prove the cell on ordinary transport:

- node management is stable over onboard 5GbE
- the current `InferenceCell` lane works on the normal Ethernet path
- restart and teardown are repeatable
- evidence is captured for D0 without depending on RDMA

This is the D0 posture. Do not block D0 on switched RoCE readiness.

## Phase 1: Point-to-Point RoCE Bring-Up

Start with two nodes only and a short SFP28 DAC.

Checklist:

- keep management on onboard 5GbE
- connect only E810 port 1 on each node
- confirm `ice` loads cleanly
- confirm `irdma` loads after `ice`
- confirm an RDMA device appears in `rdma link` and `ibv_devinfo`
- run a direct link test before introducing any switch policy complexity

Useful tests:

```bash
ethtool <fabric_if>
rdma link
ibv_devinfo
ib_write_bw -d <rdma_dev> -R
ib_write_bw -d <rdma_dev> -R <peer_ip>
```

Evidence to capture:

- PCIe negotiation
- NIC temperature and airflow notes
- link stability over repeated up/down cycles
- RDMA device visibility on both endpoints
- basic bandwidth and latency sanity results

## Phase 2: Switched RoCE Fabric

Treat this as a switch-and-policy project, not only a NIC project.

Requirements:

- switch support for the intended DCB/PFC/ECN policy
- one dedicated VLAN or traffic class for RDMA first
- clear separation between RDMA and ordinary LAN traffic during bring-up

What to validate:

- PFC configuration matches the selected traffic class
- ECN/DCQCN settings are consistent end-to-end where required
- the switch is not silently flattening priorities
- normal LAN traffic still behaves correctly on non-RDMA lanes

Do not claim production RoCE behavior until the switch policy is explicitly validated.

## Phase 3: Dual-Port Role Split

Only after port 1 is stable:

- enable E810 port 2
- assign it a distinct role such as storage, uplink, or second traffic class
- verify that enabling the second port does not destabilize the primary fabric lane

This phase is about controlled role split, not about proving simultaneous full-port saturation on an x4 host link.

## Evidence Bundle for the Roadmap

For D0 and later F4 work, keep one evidence bundle per test cycle with:

- node inventory and exact hardware identifiers
- PCIe width/speed captures
- driver and userspace versions
- direct-connect test notes
- switched-fabric policy notes
- thermal observations
- command output for `rdma link` and `ibv_devinfo`

This evidence is what turns hardware preference into roadmap truth.

## Non-Goals

This guide does not:

- make RoCE mandatory for D0
- promise full dual-port 25Gb behavior on the constrained host link
- standardize a specific case, riser, or switch SKU
- claim that Optane is part of the first public cell baseline
