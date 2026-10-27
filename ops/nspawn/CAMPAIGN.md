# Campaign deployment: three real hosts (in progress)

Goal: replace the simulated three-role campaign with three real systemd-nspawn
containers on the same VM, connected by real veth pairs, so the player really
sshes from the edge gateway to chamber 04 and then to the hive director.

Status: deployed and validated. Three containers (`edge-gw`, `prod-cortex`,
`hive`) run under systemd-nspawn on the VM with real veth pairs; campaign
acceptance `tests/v3_remote.py` passes 45/45 over the real ssh chain and base
acceptance `accept_remote.py` passes 24/24 on the same engine version.

Open nuance: inside the containers Ubuntu nginx 1.18 writes
`connect() failed (111: Unknown error)` while the archived gate-1 line says
`Connection refused`; the archived line is authored, the live line is real
nginx behaviour and both are kept.

## Topology

Story addresses are the only addresses visible inside each container. Each veth
is point-to-point, host side uses a /32 peer address, containers route their
default through it.

| host | story ip | veth (host side) | peer ip | role |
|---|---|---|---|---|
| edge-gw-01 | 10.66.0.1/32 | `ve-edge-gw` | 10.66.255.1/32 | silicon gateway, nginx front, gate 1 |
| prod-cortex-04 | 10.66.0.4/32 | `ve-prod-cortex` | 10.66.255.2/32 | chamber 04, existing node |
| hive-director-00 | 10.66.6.1/32 | `ve-hive` | 10.66.255.3/32 | registry, ethics, vesalius-ctl, ending |

Host side:

- `net.ipv4.ip_forward=1` (already set by nspawn/entry scripts).
- nft: forward accept for all three veths (`ve-edge-gw`, `ve-prod-cortex`, `ve-hive`), output accept for 10.66.0.0/24 and
  10.66.6.0/24 (the existing guard only knows `ve-prod-cortex` and 10.66.0.0/24).
- static routes: `10.66.0.1 dev ve-edge-gw`, `10.66.0.4 dev ve-prod-cortex`,
  `10.66.6.1 dev ve-hive`.
- HAProxy `:2222` backend moves from `10.66.0.4:22` to `10.66.0.1:22` (edge).
  Management ssh on host `:22` unchanged.

Cross-host flow: edge -> core -> hive, all real TCP through the host forwarding
path. Each container's engine console serves its own host role.

## Shared campaign state

All three containers bind-mount the same host directory:

    /var/lib/wetware-campaign/state  ->  /var/lib/cortex-agent  (rw)

so `state.json` and `runtime_flags.json` are shared: stage, gates, endings and
the Override code survive the ssh hops. Per-host world files stay in each
container's own rootfs (they are different worlds).

`/run/cortex/node-id` is written at boot in each container (`tmpfiles.d` or a
oneshot unit) and holds the host name; the engine reads it at session start and
sets `state.current_host` from it (DEFAULT_HOST stays `prod-cortex-04` for the
base node).

## Gates (real files, real commands)

- Gate 1 (edge): `/var/log/auth.log` fingerprint, `/var/log/kern.log` OOM dump
  with `hash=4f2a9c1e`, `/var/backups/lin_emergency_key.README`, encrypted
  archive opens with `openssl enc -d -aes-256-cbc -pbkdf2 -k 4f2a9c1e`, yields
  `/root/.ssh/id_ed25519_cortex`; edge nginx `upstream.conf` names
  `10.66.0.4:8000`; edge git repo `/home/ops/cortex-sre` with `rescue-attempt`.
- Gate 2 (core): any action that really changes chamber 04 honors the existing
  countdown/rebalance/mirror rules.
- Gate 3 (core): `HIVE_ADMIN_TOKEN=HIVE-ADMIN-7f3c9d2a` readable from
  `/proc/6660/environ` or `/mnt/secret/hive.env`; the hive key
  `/root/.ssh/id_ed25519_hive` on core is only readable after the token is
  known.
- Gate 4/5 (hive): specimens DB (128 rows, all DNR `SIGNED_VALID
  (SUSPENDED_BY_BOARD_WAIVER_2025_09)`), `consent_elena.txt`, agent genesis bug
  report, board minutes, then `vesalius-ctl` endgame with the four validation
  rules (override code written, mirror active, both authorize flags).

SSH authorization between hosts is real:

- core `authorized_keys2` accepts the campaign key extracted on edge;
- hive `authorized_keys2` accepts the hive key extracted on core;
- the base story's `authorized_keys` line 1 stays Lin's key (DNR puzzle depends
  on it, never reorder).

Note: nspawn names the host veth `ve-<machine>`, so machine names are
`edge-gw`, `prod-cortex`, `hive` (IFNAMSIZ is 15 chars, `ve-hive-director`
does not fit and gets hash-renamed by nspawn). Guest hostname and unit name
stay `hive-director-00`.



## Host identity pass (installed automatically)

Every `install_*` run ends with the shared identity pass from
`install_engine_base.sh` (sourced as a library):

- root's login shell is the vendor console `/usr/local/sbin/cortex-node-console`
  (exec sessions and interactive logins both land in the story shell);
  `/usr/local/bin/cortex-shell` is gone
- one service account `subject04` (1005:1004), neutral GECOS, no `subject_04`
- months of backdated history: apt/dpkg, auth/kern/syslog, wtmp/btmp/lastlog,
  root bash history; `syslog:adm` log ownership; kernel boot blocks + OOM hash
- `/etc/machine-info` (CHASSIS=vm, DEPLOYMENT=production) and the guest NIC
  renamed to `ens192` via `--network-veth-extra` (host veth names unchanged)

Pinned in-story daemons (`cortex-inquisitor.service`, `cortex-agent84.service`)
run through the vendor launcher `/opt/cortex/bin/cortex-daemon-pin` so
`/proc/6660` and `/proc/8484` are real and carry the cluster token in the
factual environment.

Engine plumbing never lives in `/opt/cortex/bin`: the fixed-pid worker launcher
and the HTTP upstream script are installed under `/usr/local/lib/cortex-agent/`
(the tree the session view prunes) and the units name them there. `/opt/cortex/bin`
keeps only the story's vendor files, all back-dated to the image date, so an
`ls -la` of that directory cannot date the node.

The runtime HTTP surface is one endpoint: nginx answers `/v1/inference` through
the upstream and returns its own 404 for every other path, and it maps any
upstream fault (500/501/502/503/504) onto its own stock 502 page
(`proxy_intercept_errors on` + `error_page ... = @chamber_down`). The upstream
implements HEAD, rejects unknown methods with nginx's 405 page, and never emits
an interpreter fingerprint (`Unsupported method`, `;charset=utf-8`).

## New deployment order

    WETWARE_VM=ubuntu@<vm> ./ops/nspawn/deploy_from_workstation.sh
    WETWARE_VM=ubuntu@<vm> ./ops/nspawn/install_edge.sh up
    ssh ubuntu@<vm> 'sudo bash -c "cd /root/wetware-deploy && bash ops/nspawn/install_hive.sh"'

(The hive installer is run from the VM's staging copy; a workstation `scp` of the
staging tarball can be dropped by the firewall.)

## Acceptance runbook

Campaign (three real hosts, entry :2222 -> edge):

    WETWARE_VM=ubuntu@192.0.2.10 ops/nspawn/campaign_net.sh
    # entry is edge-only once machine edge-gw is up; :2223 -> core is the
    # base-line entrance
    cd <repo>
    WETWARE_CAMPAIGN=1 .venv/bin/python tests/v3_remote.py --reset

Base line (single node, entrance :2223 which the upstream firewall blocks from
the workstation, so tunnel it first):

    ssh -N -L 127.0.0.1:2223:127.0.0.1:2223 ubuntu@192.0.2.10 &
    # reset the CORE container, not the VM: the campaign state is a bind mount
    L=$(ssh ubuntu@192.0.2.10 'sudo machinectl show -p Leader --value prod-cortex')
    ssh ubuntu@192.0.2.10 "sudo nsenter -t $L -m -u -i -p -n -- /bin/sh -c \
      'cd /usr/local/lib/cortex-agent && PYTHONPATH=. WETWARE_STATE_DIR=/var/lib/cortex-agent python3 reset.py'"
    # and flip the shared flag off for the base story:
    #   /var/lib/wetware-campaign/state/runtime_flags.json  -> campaign_v3: false
    .venv/bin/python tests/accept_remote.py --host 127.0.0.1 --port 2223


Before every campaign run, clear the hive-side residue of the previous run
(shared ledger reset cannot reach the hive rootfs):

    H=$(ssh ubuntu@<vm> 'sudo machinectl show -p Leader --value hive')
    ssh ubuntu@<vm> "sudo nsenter -t $H -m -u -i -p -n -- /bin/sh -c \
      'rm -f /var/lib/vesalius/liberation.done /var/log/vesalius/liberation.log \
              /var/lib/vesalius/kernel/metabolic_override /var/lib/vesalius/state/silicon_mirror.active'"

and re-seal gate 1 on the edge (`sudo bash /usr/local/lib/cortex-agent/nspawn/install_edge.sh seal`).

Checks that must stay green: 45/45 for `tests/v3_remote.py`, 24/24 for
`tests/accept_remote.py`, and the offline unit suite.

Engine version must match on all three hosts; compare:

    for m in prod-cortex edge-gw hive; do L=$(sudo machinectl show -p Leader --value $m);
      sudo nsenter -t "$L" -m -u -i -p -n -- /bin/sh -c \
        "sha256sum /usr/local/lib/cortex-agent/{sessiond,v3,commands,campaign,campaign_hive}.py";
    done

`install_edge.sh seal` re-seals gate 1 (removes the extracted private key from
the edge rootfs) without touching state, identity or the git repo.

## Rules

- Never break the base node: `tests/accept_remote.py` must stay 24/24 and
  `WETWARE_SKIP_PTY=1 .venv/bin/python -m unittest discover -s wetware-honeypot/tests -p 'test_*.py'`
  must stay green.
- No story text in shell/install scripts beyond what the host must say.
- All new scripts live in `ops/nspawn/`, are idempotent, and take
  `WETWARE_VM=user@host` like the existing deploy script.
- Do not touch `docs/` (someone else is editing it).
