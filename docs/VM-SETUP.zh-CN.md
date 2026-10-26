# 虚拟机环境搭建指引：三主机 nspawn 宿主机

> [English](VM-SETUP.md) | 中文 · 返回 [README](../README.zh-CN.md)

WETWARE-502 对虚拟化平台没有特定依赖，不绑定 Proxmox 或任何单一管理平台。只要满足以下配置要求的完整虚拟机均可正常运行：KVM/libvirt、Proxmox、VMware ESXi/Workstation、Hyper-V、Xen、VirtualBox，或者各大云服务器实例（阿里云 ECS、腾讯云 CVM、AWS EC2、Google Compute Engine、Azure VM、Hetzner 等）。

> **重要提示**：宿主机必须是拥有**独立 Linux 内核的完整虚拟机**。共享宿主机内核的容器环境（如非特权 Docker、LXC、OpenVZ）无法在内部运行 `systemd-nspawn` 以及挂载独立的 ext4 镜像文件，因此无法运行本系统。

---

## 1. 硬件规格与资源规划

由于宿主虚拟机内部需要同时并行运行 **三台基于 `systemd-nspawn` 的独立容器主机**（`edge-gw`、`prod-cortex` 与 `hive`），并由真实虚拟网卡（veth）跨网络互联，因此资源规格比单进程沙箱稍高：

| 配置项 | 最低要求 | 推荐配置 |
|---|---|---|
| **CPU** | 2 核 | 4 核 |
| **内存** | 4 GiB | 8 GiB |
| **磁盘空间** | 30 GiB | 50 GiB（用于容纳三台容器的 ext4 稀疏镜像） |
| **操作系统** | Ubuntu 22.04 LTS (64位) | Ubuntu 22.04.x 官方云镜像 |
| **访问权限** | 拥有 sudo / root 权限与 SSH 密钥 | 22 端口使用管理员专用密钥登录 |
| **内核版本** | 5.15 generic | 发行版自带的 5.15 或 6.x generic 内核 |
| **宿主文件系统** | ext4 根分区 | ext4（支持 `chattr +i` 及 loopback 回环挂载） |
| **初始化系统** | systemd | systemd 249 或更高版本 |
| **容器引擎** | `systemd-container` | 包含 `systemd-nspawn` 与 `machinectl` |
| **网络端口** | 可访问外网互联网 | 入站开放 22、2222、80 端口（2223 仅 VM 本机可达，需经 ssh 隧道访问） |
| **快照支持** | 推荐支持虚拟机快照 | 全部部署完成后打一份干净的基础快照 |

不需要开启 CPU 嵌套虚拟化（节点内不运行二层虚拟机，`systemd-nspawn` 基于轻量级操作系统命名空间隔离），普通的半虚拟化网卡驱动（virtio、vmxnet3）均可正常工作。

---

## 2. 宿主机内核与底层依赖

宿主虚拟机负责为三台容器提供底层运行底座：
- **容器运行时**：通过 `systemd-nspawn` 和 `machinectl` 纳管三台容器机器：
  - `edge-gw`（边缘网关容器）
  - `prod-cortex`（04号核心舱，3.5 GiB ext4 稀疏镜像 `/var/lib/wetware-hosts/prod-cortex-04.img`）
  - `hive`（中央总控容器）
- **内核网络平面**：
  - 开启 IPv4 数据包转发（`net.ipv4.ip_forward=1`）。
  - 点对点虚拟以太网卡对（`veth`）：`ve-edge-gw`、`ve-prod-cortex`、`ve-hive`。
  - 点对点 `/32` 静态路由表。
  - 宿主机内部使用 `nftables` 或 `iptables` 规则实现端口转发与跨容器路由。
- **共享状态挂载**：
  - 将宿主机目录 `/var/lib/wetware-campaign/state` 以读写模式挂载到各容器的 `/var/lib/cortex-agent`。
- **回环块设备支持**：
  - 启用 loop 设备驱动（`/dev/loop*`），用于挂载容器的 ext4 磁盘镜像。
- **依赖软件包**：
  - `debootstrap`、`systemd-container`、`curl`、`python3`、`python3-venv`、`gcc`、`e2fsprogs`。

---

## 3. 网络端口规划与转发路径

```
22 端口   (SSH)  ：宿主机专属管理端口（密钥登录，供管理员维护宿主机）
2222 端口 (SSH)  ：战役排障统一入口（通过 nftables 转发给 edge-gw 容器: 10.66.0.1:22）
2223 端口 (SSH)  ：核心节点直连入口（转发给 prod-cortex 容器: 10.66.0.4:22，用于单机基线测试）
80 端口   (HTTP) ：对外 Web 访问端口（转发给 edge-gw 容器: 10.66.0.1:80）
容器间互访       ：宿主机内核在 10.66.0.1、10.66.0.4 与 10.66.6.1 之间进行真实 TCP 路由转发
出站安全限制     ：虚拟机防火墙默认拦截私有网段（RFC1918），仅放行公网互联网访问
```

---

## 4. 搭建步骤检查清单

1. **创建宿主虚拟机**：使用 Ubuntu 22.04 LTS 官方镜像创建一台至少 4 GiB 内存、30 GiB 磁盘的虚拟机。
2. **确认基础访问**：
   ```bash
   ssh -i ~/.ssh/id_rsa ubuntu@<vm-ip>
   sudo -v
   ```
3. **执行宿主机初始化**：
   ```bash
   scp ops/bootstrap-node.sh ubuntu@<vm-ip>:/tmp/
   ssh ubuntu@<vm-ip> 'sudo WETWARE_VM_IP=<vm-ip> bash /tmp/bootstrap-node.sh'
   ```
4. **从开发机部署三主机容器网格**：
   ```bash
   # 同步部署脚本
   WETWARE_VM=ubuntu@<vm-ip> ./ops/nspawn/deploy_from_workstation.sh
   # 构建核心舱镜像
   ssh ubuntu@<vm-ip> 'sudo -n bash /usr/local/lib/cortex-agent/nspawn/build_base.sh up'
   # 配置三容器网络网格
   WETWARE_VM=ubuntu@<vm-ip> ./ops/nspawn/campaign_net.sh
   # 部署边缘网关与中央总控
   ssh ubuntu@<vm-ip> 'sudo -n bash /usr/local/lib/cortex-agent/nspawn/install_edge.sh'
   ssh ubuntu@<vm-ip> 'sudo -n bash /usr/local/lib/cortex-agent/nspawn/install_hive.sh'
   ```
5. **运行自动化验证**：
   ```bash
   # 验证三主机战役链条（2222 端口）
   WETWARE_CAMPAIGN=1 .venv/bin/python tests/v3_remote.py --reset
   # 验证核心舱单机基线（2223 端口）。上游防火墙只放行 22/80/2222，
   # 先从工作站做 ssh 本地转发：
   ssh -N -L 127.0.0.1:2223:127.0.0.1:2223 ubuntu@<vm-ip> &
   .venv/bin/python tests/accept_remote.py --host 127.0.0.1 --port 2223
   ```
6. **创建干净快照**：验证通过后，在虚拟化管理面板上创建系统快照，便于随时一键回滚。
