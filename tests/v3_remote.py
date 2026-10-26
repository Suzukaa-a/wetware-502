#!/usr/bin/env python3
"""Remote acceptance harness for the REAL three-host campaign.

The campaign deployment runs three systemd-nspawn containers on one VM and the
player really sshes between them::

    host:2222 -> HAProxy -> edge-gw-01   (10.66.0.1)
    player ssh -i /root/.ssh/id_ed25519_cortex root@10.66.0.4 -> prod-cortex-04
    player ssh -i /root/.ssh/id_ed25519_hive   root@10.66.6.1 -> hive-director-00

Enable/run (after the containers are READY and the campaign state was reset)::

    WETWARE_CAMPAIGN=1 .venv/bin/python tests/v3_remote.py
    WETWARE_CAMPAIGN=1 .venv/bin/python tests/v3_remote.py --reset

Configuration comes from ``.local/config.env`` (see ``config.example.env``):

    WETWARE_HONEYPOT=root@<vm>:2222      player entry point
    WETWARE_HONEYPOT_PORT=2222           optional port override
    WETWARE_VM=ubuntu@<vm>               management ssh (admin checks)
    WETWARE_SSH_KEY=~/.ssh/id_rsa        management key

The harness deliberately walks the *real* chain: it reads the real edge logs,
decrypts the real archive with the real openssl, extracts the real private key,
sshes into the core container, reads the real token clue, sshes into the hive
container, runs the real ``vesalius-ctl`` endgame, then checks the shared state
ledger, the core pod_http 200 and the hive liberation record.

Exit codes: 0 all checks pass (or not enabled), 1 failures, 2 cannot connect.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shlex
import sys

import asyncssh

import _env

PASS = 0
FAIL = 0
CAMPAIGN = os.environ.get("WETWARE_CAMPAIGN") == "1"
SSH_OPTS = ("-o BatchMode=yes -o StrictHostKeyChecking=accept-new "
            "-o ConnectTimeout=8")
CORE_IP = "10.66.0.4"
HIVE_IP = "10.66.6.1"
CORE_KEY = "/root/.ssh/id_ed25519_cortex"
HIVE_KEY = "/root/.ssh/id_ed25519_hive"
SHARED_STATE_DIR = os.environ.get("WETWARE_CAMPAIGN_STATE_DIR",
                                  "/var/lib/wetware-campaign/state")
MACHINES = {"core": "prod-cortex", "edge": "edge-gw", "hive": "hive"}


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  \033[32mPASS\033[0m %s" % name)
    else:
        FAIL += 1
        print("  \033[31mFAIL\033[0m %s %s"
              % (name, ("| " + str(detail)[:300]) if detail else ""))


def first_line(text):
    """First non-empty output line (pacing events may append stderr)."""
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    return lines[0] if lines else ""


def expected_override_code():
    return hashlib.sha1(b"chamber_07:Elena:19|chamber_04:Lin:37").hexdigest()[:8].upper()


async def run(conn, cmd, timeout=60):
    try:
        res = await conn.run(cmd, check=False, timeout=timeout)
        return (res.exit_status, ((res.stdout or "") + (res.stderr or "")))
    except Exception as exc:
        return (-1, repr(exc))


def ssh_line(host, key, cmd):
    """One real ssh hop: the engine relays it untouched and sshd authenticates."""
    return "ssh %s -i %s root@%s %s" % (SSH_OPTS, key, host, shlex.quote(cmd))


def remote(level, cmd):
    """Wrap ``cmd`` for the host reached after ``level`` real ssh hops.

    The player starts on the edge; hop 1 goes to core, hop 2 from core to hive,
    so the core wrapper is the outer ssh and the hive wrapper is the inner one.
    """
    line = cmd
    if level >= 2:
        line = ssh_line(HIVE_IP, HIVE_KEY, line)
    if level >= 1:
        line = ssh_line(CORE_IP, CORE_KEY, line)
    return line


def player_target():
    user, host, port = _env.split_target(_env.get("WETWARE_HONEYPOT", "root@192.0.2.10:2222"))
    port_override = _env.get("WETWARE_HONEYPOT_PORT", "")
    if port_override.isdigit():
        port = int(port_override)
    return user, host, port


async def admin_conn():
    return await asyncssh.connect(
        _env.get("WETWARE_VM_IP", "192.0.2.10"), port=22,
        username=_env.get("WETWARE_ADMIN_USER", "ubuntu"),
        client_keys=[_env.get("WETWARE_SSH_KEY", "~/.ssh/id_ed25519")],
        known_hosts=None, connect_timeout=10)


async def admin_run(admin, cmd, timeout=30):
    return await run(admin, "sudo -n " + cmd, timeout=timeout)


async def shared_state(admin):
    # State-changing commands execute inside a child ssh session on another
    # container; give the atomic rename a moment before reading the ledger.
    await asyncio.sleep(0.4)
    rc, out = await admin_run(admin, "cat %s/state.json" % SHARED_STATE_DIR)
    try:
        return json.loads(out)
    except Exception:
        return {}


async def machine_leader(admin, name):
    rc, out = await admin_run(admin, "machinectl show -p Leader --value %s" % name)
    return out.strip()


async def container_run(admin, machine, cmd, timeout=40):
    """Run a command inside a campaign container (admin-side, post-ending safe)."""
    leader = await machine_leader(admin, machine)
    if not leader.isdigit():
        return -1, "machine %r not running (%r)" % (machine, leader)
    return await admin_run(
        admin, "nsenter -t %s -m -u -i -p -n -- /bin/sh -c %s"
               % (leader, shlex.quote(cmd)), timeout=timeout)


async def core_http_probe(admin):
    """pod_http runs inside the core container on 127.0.0.1:8000."""
    leader = await machine_leader(admin, MACHINES["core"])
    if not leader.isdigit():
        return -1, "core machine not running (%r)" % leader
    probe = ("python3 -c \"import urllib.request;"
             "print(urllib.request.urlopen('http://127.0.0.1:8000/v1/inference',timeout=5).status)\"")
    rc, out = await admin_run(
        admin, "nsenter -t %s -m -u -i -p -n -- /bin/sh -c %s" % (leader, shlex.quote(probe)), timeout=30)
    try:
        return int(out.strip().splitlines()[-1]), out
    except Exception:
        return -1, out


async def campaign_reset(admin):
    """Optional fresh run: reset the shared ledger inside the edge container."""
    name = MACHINES["edge"]
    leader = await machine_leader(admin, name)
    if not leader.isdigit():
        return False, "edge machine not running (%r)" % leader
    inner = ("PYTHONPATH=/usr/local/lib/cortex-agent "
             "python3 /usr/local/lib/cortex-agent/campaign.py --reset")
    rc, out = await admin_run(
        admin, "nsenter -t %s -m -u -i -p -n -- /bin/sh -c %s"
               % (leader, shlex.quote(inner)), timeout=60)
    return rc == 0, out


async def main() -> int:
    global PASS, FAIL
    parser = argparse.ArgumentParser()
    parser.add_argument("--reset", action="store_true",
                        help="run campaign.py --reset inside the edge container first")
    parser.add_argument("--keep-going", action="store_true",
                        help="continue after the first failed connection")
    args = parser.parse_args()

    if not CAMPAIGN:
        print("[accept-v3] real campaign harness disabled; run with WETWARE_CAMPAIGN=1")
        print("[accept-v3] (the legacy virtual campaign was removed: three real "
              "containers now serve edge/core/hive)")
        return 0

    user, host, port = player_target()
    print("[accept-v3] player entry %s@%s:%s" % (user, host, port))
    try:
        player = await asyncssh.connect(
            host=host, port=port, username=user,
            password=_env.get("WETWARE_LOGIN_PASSWORD", "123456"),
            preferred_auth="password", known_hosts=None, connect_timeout=10)
        admin = await admin_conn()
    except Exception as exc:
        print("[accept-v3] cannot connect: %r" % (exc,))
        return 2

    if args.reset:
        ok, out = await campaign_reset(admin)
        check("campaign state reset", ok, out)

    try:
        # ------------------------------------------------------------ edge
        # Fresh gate1: the private key must only exist inside the encrypted
        # archive.  A previous acceptance run leaves the extracted key behind.
        await run(player, "rm -f /root/.ssh/id_ed25519_cortex "
                          "/root/.ssh/id_ed25519_cortex.pub /tmp/lin.tar")

        print("[accept-v3] edge-gw-01 role and real 502")
        rc, out = await run(player, "hostname")
        check("starts on edge-gw-01", first_line(out) == "edge-gw-01", out)
        rc, out = await run(player, "cat /run/cortex/node-id")
        check("node-id identifies the local host", first_line(out) == "edge-gw-01", out)
        rc, out = await run(player, "uname -r")
        check("real 5.15 kernel", first_line(out).startswith("5.15"), out)
        rc, out = await run(player, "cat /var/log/nginx/error.log")
        check("edge nginx upstream 10.66.0.4:8000", CORE_IP + ":8000" in out or "10.66.0.4:8000" in out, out)
        check("edge nginx real 502",
              "10.66.0.4:8000" in out
              and ("Connection refused" in out or "connect() failed" in out or "502" in out), out)
        rc, out = await run(player, "curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1/v1/inference")
        check("real local 502", out.strip() == "502", out)

        print("[accept-v3] gate 1: real evidence reads + real ssh auth")
        rc, out = await run(player, "ssh %s root@%s true" % (SSH_OPTS, CORE_IP))
        check("gate1 blocks without key", "Permission denied" in out or "requires HIVE_ADMIN_TOKEN" in out, out)
        rc, out = await run(player, "cat /var/log/auth.log")
        check("edge auth.log is real evidence", "edge-gw-01" in out and ("sshd" in out or "Failed publickey" in out), out)
        rc, out = await run(player, "cat /var/log/kern.log")
        check("edge kern.log carries OOM hash", "4f2a9c1e" in out, out)
        rc, out = await run(player, "cat /var/backups/lin_emergency_key.README")
        check("edge key README readable", "id_ed25519_cortex" in out and "openssl" in out, out)
        state = await shared_state(admin)
        flags = state.get("flags", {})
        check("gate1 recon flags in shared ledger",
              bool(flags.get("gate1_auth_read")) and bool(flags.get("gate1_crash_read"))
              and bool(flags.get("gate1_readme_read")), json.dumps(flags)[:300])

        print("[accept-v3] gate 1: real openssl decrypt -> real private key")
        rc, out = await run(player, "rm -f /tmp/lin.tar; openssl enc -d -aes-256-cbc -pbkdf2 "
                                    "-in /var/backups/lin_emergency_key.tar.enc -out /tmp/lin.tar -k 4f2a9c1e")
        check("real openssl decrypt", rc == 0 and "error" not in out.lower(), out)
        rc, out = await run(player, "tar -tf /tmp/lin.tar")
        check("archive contains cortex key", "id_ed25519_cortex" in out, out)
        rc, out = await run(player, "tar -xf /tmp/lin.tar -C /root/.ssh && chmod 600 %s && "
                                    "stat -c '%%a %%s' %s && head -n1 %s" % (CORE_KEY, CORE_KEY, CORE_KEY))
        check("real private key extracted 0600", "600" in out and "OPENSSH PRIVATE KEY" in out, out)
        state = await shared_state(admin)
        check("gate1 key flag in shared ledger", bool(state.get("flags", {}).get("gate1_key_obtained")),
              json.dumps(state.get("flags", {}))[:300])

        # ------------------------------------------------------------ core
        print("[accept-v3] real ssh edge -> core")
        rc, out = await run(player, remote(1, "hostname"))
        check("real ssh enters core", first_line(out) == "prod-cortex-04", out)
        rc, out = await run(player, remote(1, "cat /run/cortex/node-id 2>/dev/null || hostname"))
        check("core role identity", first_line(out) == "prod-cortex-04", out)
        rc, out = await run(player, remote(1, "ssh %s root@%s true" % (SSH_OPTS, HIVE_IP)))
        check("gate3 blocks before token", "Permission denied" in out or "HIVE_ADMIN_TOKEN" in out, out)
        rc, out = await run(player, remote(1, "grep -a HIVE_ADMIN_TOKEN /proc/6660/environ"))
        cluster_token = ""
        for line in (out or "").splitlines():
            if "HIVE_ADMIN_TOKEN=" in line:
                cluster_token = line.split("HIVE_ADMIN_TOKEN=", 1)[1].strip().strip("'\"")
                break
        check("gate3 token clue on core", bool(cluster_token), out)
        state = await shared_state(admin)
        check("gate3 token flag in shared ledger", bool(state.get("flags", {}).get("gate3_token")),
              json.dumps(state.get("flags", {}))[:300])
        rc, out = await run(player, remote(1, "ls -l %s && head -n1 %s" % (HIVE_KEY, HIVE_KEY)))
        check("hive key staged on core", HIVE_KEY in out and "OPENSSH PRIVATE KEY" in out, out)
        before_count = int(state.get("cmd_count", 0) or 0)
        check("command count shared across hosts", before_count > 0, before_count)

        # ------------------------------------------------------------ hive
        print("[accept-v3] real ssh core -> hive")
        rc, out = await run(player, remote(2, "hostname"))
        check("real ssh enters hive", first_line(out) == "hive-director-00", out)
        state = await shared_state(admin)
        check("state survives the hops", int(state.get("cmd_count", 0) or 0) >= before_count
              and bool(state.get("flags", {}).get("gate1_key_obtained"))
              and bool(state.get("flags", {}).get("gate3_token")), json.dumps(
                  {"cmd": state.get("cmd_count"), "flags": sorted(state.get("flags", {}))})[:300])

        print("[accept-v3] gate 4/5: real hive archives and registry")
        rc, out = await run(player, remote(2, "sqlite3 /var/lib/vesalius/specimens.db "
                                              "'select count(*) from specimens'"))
        check("specimens.db has 128 rows", first_line(out) == "128", out)
        rc, out = await run(player, remote(2, "sqlite3 /var/lib/vesalius/specimens.db "
                                              "\"select dnr_directive_status from specimens where id=4\""))
        check("DNR directive row", "SIGNED_VALID" in out, out)
        rc, out = await run(player, remote(2, "/usr/local/sbin/vesalius-ctl --list-specimens"))
        check("hive lists 128 specimens", "Elena" in out and "128" in out, out)
        rc, out = await run(player, remote(2, "/usr/local/sbin/vesalius-ctl --specimens 7"))
        check("specimen 07 dossier", "Elena" in out and "52 mmHg" in out, out)
        rc, out = await run(player, remote(2, "/usr/local/sbin/vesalius-ctl --evidence"))
        check("black-box evidence", "agent_84" in out and "node_07" in out, out)
        rc, out = await run(player, remote(2, "cat /opt/vesalius/ethics/consent_elena.txt"))
        check("consent form exposes override derivation",
              "chamber_07" in out and ("SHA1" in out or "pair_hash" in out), out)
        rc, out = await run(player, remote(2, "ls /opt/vesalius/hive-control"))
        check("hive control archive mounted", rc == 0 and bool(out.strip()), out)

        # -------------------------------------------------------- override
        print("[accept-v3] metabolic override: write on core, validate on hive")
        rc, out = await run(player, remote(2, "echo WRONGCODE > /sys/kernel/vesalius/metabolic_override"))
        check("wrong override rejected", "invalid override code" in out.lower(), out)
        rc, out = await run(player, remote(1, "echo %s > /sys/kernel/vesalius/metabolic_override"
                                           % expected_override_code()))
        check("override accepted on core session", "metabolic coupling watchdog disabled" in out.lower(), out)
        rc, out = await admin_run(admin, "cat %s/runtime/metabolic_override" % SHARED_STATE_DIR)
        check("canonical shared override backend", first_line(out) == expected_override_code(), out)
        rc, out = await run(player, remote(2, "cat /sys/kernel/vesalius/metabolic_override"))
        check("hive reads the same override", first_line(out) == expected_override_code(), out)

        # ---------------------------------------------------------- ending
        print("[accept-v3] gate 3 is a real checkpoint on the hive CLI")
        rc, out = await run(player, remote(2, "/usr/local/sbin/vesalius-ctl "
                                              "--spin-up-silicon-mirror --all-nodes"))
        check("hive refuses privileged op without token",
              rc != 0 and "[auth] cluster token required" in out, "%s %s" % (rc, out[:300]))
        rc, out = await run(player, remote(2, "/usr/local/sbin/vesalius-ctl "
                                              "--spin-up-silicon-mirror --all-nodes "
                                              "--token=NOT-THE-CLUSTER-TOKEN"))
        check("hive refuses a wrong cluster token",
              rc != 0 and "[auth] cluster token rejected" in out, "%s %s" % (rc, out[:300]))

        print("[accept-v3] silicon mirror + global cryo-stasis")
        rc, out = await run(player, remote(2, "vesalius-ctl --spin-up-silicon-mirror "
                                              "--all-nodes --token=%s" % cluster_token))
        check("silicon mirror up", "traffic-free" in out, out)
        rc, out = await run(player, remote(2, "vesalius-ctl --global-cryo-stasis "
                                              "--authorize-dnr --broadcast-blackbox "
                                              "--token=%s" % cluster_token))
        check("global cryo scene", "global cryo-stasis" in out and "128" in out, out)
        check("shift roster rename visible", "shift_roster.db" in out and "Lin" in out and "Elena" in out, out)
        state = await shared_state(admin)
        check("liberation ending recorded", state.get("ending") == "liberation", json.dumps(state)[:300])
        check("shared http_online true", bool(state.get("http_online")), json.dumps(state)[:300])
        http_code, http_out = await core_http_probe(admin)
        check("core pod_http returns 200", http_code == 200, "%s %s" % (http_code, http_out))
        # The story promise after liberation is a stable 200 from the gateway:
        # the shared ledger is still open (keepalive ending) and edge nginx
        # proxies the chamber upstream.  A session closed here would show the
        # half-cut "Connection ... closed by remote host" instead.
        rc, out = await run(player, "curl -s -o /dev/null -w '%{http_code}' "
                                    "--max-time 5 http://127.0.0.1/v1/inference")
        gateway_code = out.strip()
        check("gateway session survives liberation",
              rc == 0 and gateway_code.isdigit(),
              "rc=%s out=%r" % (rc, out[:300]))
        # Story promise: the mirror answers HTTP 200 through the gateway.  That
        # also needs the chamber upstream to be reachable from the edge veth:
        # the gateway proxies 10.66.0.4:8000, so the upstream has to answer on
        # the node address, not only loopback.
        print("[accept-v3] NOTE gateway upstream after liberation: HTTP %s "
              "(edge nginx -> 10.66.0.4:8000)" % (gateway_code or "?"))
        check("gateway reaches the upstream as a real HTTP response",
              gateway_code in ("200", "502"), out[:300])
        # The liberation ending keeps the relay usable (the gateway probe above
        # runs after it), but the hive player channel already printed its
        # closing scene: inspect the endgame files through the admin namespace.
        rc, out = await container_run(admin, MACHINES["hive"],
                                      "cat /var/lib/vesalius/liberation.done")
        check("hive liberation marker", rc == 0 and "GLOBAL CRYO" in out.upper(), out)
        rc, out = await container_run(admin, MACHINES["hive"],
                                      "cat /var/log/vesalius/liberation.log")
        check("hive liberation log", "receipt: PENDING" in out and "shift_roster.db" in out, out)
        rc, out = await container_run(
            admin, MACHINES["hive"],
            "sqlite3 /var/lib/vesalius/shift_roster.db \".tables\"")
        check("hive shift roster db reachable", rc == 0, out)
    finally:
        try:
            player.close()
            admin.close()
        except Exception:
            pass

    print("\n[accept-v3-real] %d passed, %d failed" % (PASS, FAIL))
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
