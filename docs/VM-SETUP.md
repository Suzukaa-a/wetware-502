# Creating a Compatible Virtual Machine: Three-Host nspawn Host

> English | [中文](VM-SETUP.zh-CN.md) · Back to [README](../README.md)

WETWARE-502 is hypervisor-agnostic. Any full-system virtual machine satisfying the specifications below will work: KVM/libvirt, Proxmox, VMware ESXi/Workstation, Hyper-V, Xen, VirtualBox, or standard cloud instances (AWS EC2, Google Compute Engine, Azure VM, Alibaba Cloud ECS, Hetzner, etc.).

> **Important**: The host must be a **full virtual machine with its own dedicated Linux kernel**. Shared-kernel container environments (such as unprivileged Docker, LXC, or OpenVZ) cannot run `systemd-nspawn` with independent PID 1 init systems and dedicated loopback ext4 image filesystems.

---

## 1. Hardware & Resource Sizing

Because the host VM runs **three concurrent `systemd-nspawn` containers** (`edge-gw`, `prod-cortex`, and `hive`) connected by real virtual ethernet pairs, sizing requirements are slightly larger than a single-process sandbox:

| Parameter | Minimum Requirement | Recommended Specification |
|---|---|---|
| **vCPU** | 2 cores | 4 cores |
| **RAM** | 4 GiB | 8 GiB |
| **Disk** | 30 GiB | 50 GiB (sparse ext4 container images) |
| **Operating System** | Ubuntu 22.04 LTS (64-bit) | Ubuntu 22.04.x official cloud image |
| **Access Privileges** | sudo / root access with SSH key | Dedicated management key on port 22 |
| **Kernel Version** | 5.15 generic | Distribution 5.15 / 6.x generic |
| **Host Filesystem** | ext4 root partition | ext4 (`chattr +i` and loopback device support) |
| **Init System** | systemd | systemd 249 or later |
| **Container Engine** | `systemd-container` | `systemd-nspawn` package with `machinectl` |
| **Network Interfaces** | Outbound public internet access | Inbound ports 22, 2222 and 80 (2223 stays VM-local; reach it through an ssh tunnel) |
| **Snapshot Support** | Recommended for clean rollbacks | Baseline clean snapshot taken after deployment |

Nested virtualization features are **not** required, as `systemd-nspawn` uses OS-level namespace containerization rather than inner hardware virtual machines.

---

## 2. Host Operating System Features Required

The host VM provides the infrastructure plane for the three containers:
- **Container Runtime**: `systemd-nspawn` and `machinectl` managing three machines:
  - `edge-gw` (3.5 GiB loopback image or debootstrapped tree)
  - `prod-cortex` (3.5 GiB sparse ext4 loopback image `/var/lib/wetware-hosts/prod-cortex-04.img`)
  - `hive` (3.5 GiB loopback image or debootstrapped tree)
- **Kernel Networking**:
  - IPv4 packet forwarding enabled (`net.ipv4.ip_forward=1`).
  - Virtual Ethernet pairs (`veth`): `ve-edge-gw`, `ve-prod-cortex`, and `ve-hive`.
  - Point-to-point `/32` static routing.
  - In-host packet filtering and NAT via `nftables` or `iptables`.
- **Shared State Mount**:
  - Host directory `/var/lib/wetware-campaign/state` bind-mounted read-write into each container at `/var/lib/cortex-agent`.
- **Loopback Support**:
  - Loopback block device drivers (`/dev/loop*`) for mounting container ext4 disk images.
- **Host Tools**:
  - `debootstrap`, `systemd-container`, `curl`, `python3`, `python3-venv`, `gcc`, `e2fsprogs`.

---

## 3. Host Network Layout & Port Mapping

```
Port 22   (SSH)  : Host management interface (key-only, direct access to VM)
Port 2222 (SSH)  : Campaign triage entrypoint (forwards via nftables to edge-gw: 10.66.0.1:22)
Port 2223 (SSH)  : Baseline core entrypoint (forwards via nftables to prod-cortex: 10.66.0.4:22)
Port 80   (HTTP) : External Web endpoint (forwards to edge-gw: 10.66.0.1:80)
Container Mesh   : Host forwards traffic between 10.66.0.1, 10.66.0.4, and 10.66.6.1
Egress Traffic   : In-guest firewall blocks internal RFC1918 subnets while permitting public internet access
```

---

## 4. Provisioning Checklist

1. **Deploy Host VM**: Launch a fresh Ubuntu 22.04 LTS instance with at least 4 GiB RAM and 30 GiB disk.
2. **Verify Access**:
   ```bash
   ssh -i ~/.ssh/id_rsa ubuntu@<vm-ip>
   sudo -v
   ```
3. **Execute Host Bootstrap**:
   ```bash
   scp ops/bootstrap-node.sh ubuntu@<vm-ip>:/tmp/
   ssh ubuntu@<vm-ip> 'sudo WETWARE_VM_IP=<vm-ip> bash /tmp/bootstrap-node.sh'
   ```
4. **Deploy Containers & Campaign Network**:
   ```bash
   # From local workstation
   WETWARE_VM=ubuntu@<vm-ip> ./ops/nspawn/deploy_from_workstation.sh
   ssh ubuntu@<vm-ip> 'sudo -n bash /usr/local/lib/cortex-agent/nspawn/build_base.sh up'
   WETWARE_VM=ubuntu@<vm-ip> ./ops/nspawn/campaign_net.sh
   ssh ubuntu@<vm-ip> 'sudo -n bash /usr/local/lib/cortex-agent/nspawn/install_edge.sh'
   ssh ubuntu@<vm-ip> 'sudo -n bash /usr/local/lib/cortex-agent/nspawn/install_hive.sh'
   ```
5. **Run Verification**:
   ```bash
   # Multi-host campaign (port 2222)
   WETWARE_CAMPAIGN=1 .venv/bin/python tests/v3_remote.py --reset
   # Single-node baseline (port 2223). The upstream firewall only opens
   # 22/80/2222, so forward 2223 over ssh from the workstation first:
   ssh -N -L 127.0.0.1:2223:127.0.0.1:2223 ubuntu@<vm-ip> &
   .venv/bin/python tests/accept_remote.py --host 127.0.0.1 --port 2223
   ```
6. **Take Hypervisor Snapshot**: Once verified, snapshot the host VM state for rapid rollbacks.
