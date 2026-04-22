# AI Max+ 395 Hardware Baseline

Status: canonical public node baseline for the first AMD fabric cell.

This page defines the exact public hardware baseline for the near-term AI Max+ 395 fabric path. It complements [Inference Fabric](inference-fabric.html), the [Distributed Compute Fabric Roadmap](distributed-compute-fabric.html), and [AI Max+ 395 Cluster Prep](ai-max-395-cluster-prep.html).

## Summary

The first public hardware target for the fabric program is one 4-node AI Max+ 395 cell built from a repeatable node profile.

AMD's public AI Max cluster guidance for local large-model inference is the main public reason this baseline uses a 4-node cell shape. See AMD's article: [How to run a one-trillion-parameter LLM locally on an AMD Ryzen AI Max+ cluster](https://www.amd.com/en/developer/resources/technical-articles/2026/how-to-run-a-one-trillion-parameter-llm-locally-an-amd.html).

This baseline makes four rules explicit:

- standard Ethernet remains the correctness baseline for the current substrate
- the RoCE path is documented now, but it remains a formal F4 acceleration target rather than a D0 requirement
- one storage class is preferred for the baseline cell so node behavior is easier to compare
- exact SKU choices live here, while control-plane logic remains capability-driven

## Canonical Node Profile

| Area | Canonical choice | Notes |
| --- | --- | --- |
| Compute node | Framework Desktop mainboard with AMD Ryzen AI Max+ 395 | Mini-ITX form factor with a PCIe x4 slot; current public target is the Framework Desktop platform |
| Cell size | 4 nodes | The near-term execution unit follows AMD's published AI Max cluster proof point |
| Baseline NVMe | Micron 7450 MAX M.2 2280 | Canonical cluster-wide SSD class when one NVMe type is used |
| Management NIC | onboard 5GbE | Management, provisioning, and fallback path |
| Fabric NIC | Intel E810-XXVDA2 | Canonical RoCE development NIC |
| First interconnect medium | SFP28 DAC | Start with DACs before optics |
| OS posture | modern Linux distro | Use a kernel/userspace combination that supports `ice`, `irdma`, and `rdma-core` cleanly |

## Network Role Split

The node networking split is intentional:

- onboard 5GbE handles management, provisioning, and fallback access
- E810 port 1 is the primary fabric / RoCE development port
- E810 port 2 stays disabled at first and is reserved for later role split, storage, uplink, or a second traffic domain

This keeps the first hardware story simple and prevents the early fabric path from depending on dual-port saturation assumptions.

## Storage Baseline

If one SSD class is used across the first public cell, it should be the [Micron 7450 SSD](https://www.micron.com/products/storage/ssd/data-center-ssd/7450-ssd) in the `7450 MAX M.2 2280` form factor. Micron's published product material lists `7450 MAX M.2 2280` variants in its [technical product specification](https://www.micron.com/content/dam/micron/global/public/documents/products/technical-marketing-brief/7450-nvme-ssd-tech-prod-spec.pdf).

Why this is the baseline:

- it is a data-center SSD family rather than a consumer scratch drive
- the form factor aligns with the Framework Desktop storage path
- using one SSD class across the first cell improves repeatability for boot, cache, and restart testing

This document does not require every future node family to use the same SSD. It only fixes the first public AI Max cell baseline.

## RoCE Development Baseline

The current RoCE development target is the [Intel E810-XXVDA2 product family](https://www.intel.com/content/www/us/en/products/sku/189760/intel-ethernet-network-adapter-e810xxvda2/specifications.html). Intel's product brief documents the adapter as a PCIe 4.0 x8, dual-port 25/10/1GbE SFP28 adapter with both [iWARP and RoCEv2 support](https://cdrdv2-public.intel.com/641674/Intel%20Ethernet%20Network%20Adapter%20E810-XXVDA2%20Product%20Brief.pdf).

Framework's public Desktop materials describe the platform as Mini-ITX with a [PCIe x4 slot and 2x NVMe PCIe 4.0 x4 M.2 2280 sockets](https://frame.work/desktop/?tab=specs). Framework's [Secondary Storage guide](https://guides.frame.work/Guide/Secondary%2BStorage/504) covers the Desktop storage path directly.

Intel support has also stated in an official Intel community thread that the E810 can negotiate down on PCIe 4.0 x4 hosts in supported cases and can negotiate to `x4` or `x1` when constrained by slot width. Treat that as a supported constrained mode, not as evidence that an x4 host behaves like a native x8 host under all dual-port load patterns. See the Intel support thread: [E810-XXVDA2 running at PCIe x4](https://community.intel.com/t5/Ethernet-Products/E810-XXVDA2-running-at-PCIe-x4/m-p/1451852).

Public hardware guidance for this path is therefore:

- one E810 per node is the canonical RoCE development option
- DAC first, optics later
- one RoCE port first, second port later
- do not claim guaranteed full dual-port 25Gb saturation on the x4 host link

## Mechanical and Thermal Constraints

The baseline build assumes:

- a standard Mini-ITX enclosure or equivalent open test layout
- physical clearance for the E810 card and bracket
- directed airflow across the NIC heatsink
- room for the Framework-provided storage heatspreaders

This page does not lock a specific case or riser SKU because none has been canonized yet. The requirement is fit, cooling, and repeatability, not a particular enclosure vendor.

## Software Stack Expectations

The RoCE development stack is expected to follow Intel's published Linux guidance:

- bring up the `ice` LAN driver first
- load `irdma` only after the LAN path is healthy
- use `rdma-core` userspace tools for verification
- isolate RDMA traffic into an intentional VLAN or traffic class when moving beyond direct-connect testing

Relevant Intel references:

- [Intel Ethernet 800 Series RDMA Ease of Use for Linux](https://cdrdv2-public.intel.com/788116/788116_Intel%C2%AE%20Ethernet%20800%20Series%20RDMA%20Ease%20of%20Use%20Application%20Note_rev1_2.pdf)
- [Intel Ethernet 800 Series Linux Flow Control for RDMA Use Cases](https://cdrdv2-public.intel.com/635330/635330_800%20Series%20Linux%20Flow%20Control%20for%20RDMA%20Use%20Cases_rev1_4.pdf)

## Optane Exploratory Annex

Intel Optane M.2 118G modules remain interesting as a possible second storage class for later persistent or warm-tier experiments, but they are not part of the canonical public baseline.

Current public position:

- Optane is exploratory only
- availability and sourcing are unstable enough that it should not appear in D0 exit criteria
- any future Optane path belongs to later typed-fact and locality work, not the initial cell baseline

## Relationship to the Roadmap

This hardware baseline does not change the phase order:

- D0 remains the first repeatable AI Max+ 395 cell on standard transport
- F1 is where the controller should learn typed facts about storage media, PCIe state, RNIC family, and RDMA capability
- F4 is where RoCE becomes a formal acceleration milestone instead of a prep note

For the actionable bring-up sequence, use [AI Max+ 395 Cluster Prep](ai-max-395-cluster-prep.html).
