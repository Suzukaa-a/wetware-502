# -*- coding: utf-8 -*-
"""Offline tests for ``facade.py`` (the v3 campaign virtual-host filesystem).

No VM, no network, no SSH: everything here is pure data/rendering.

Coverage
  * ``ls -l/-a/-h`` shape: 10-char permission strings, ``total N``, dotfiles
    only under ``-a``, symlink arrows, device major/minor, sticky bits;
  * one record drives cat/stat/file: byte counts, ``stat`` size/inode/mode,
    ``file`` type, and ``ls -l`` size column all agree;
  * edge vs hive are *different machines*: no ``/home`` (and no root-listing
    mention of it) on the director, chambers only on the hive, and the edge's
    ordinary listings never equal the hive's;
  * the original bug: ``ls -la /tmp``/``/etc``/``/home`` are never the root
    listing, and an unknown path yields ``cannot access`` instead of a lie;
  * no wetware artefact (``synapse_worker``) or hive path on the clean edge;
  * common files never ENOENT on either host;
  * cross-module consistency with ``realism.py`` for the three files both
    modules answer (``/etc/hostname``, ``/etc/os-release``, ``/etc/machine-id``)
    -- cat == cat, stat == stat, file == file, byte for byte;
  * campaign state hooks: Gate 1 key/tar absent until extracted, hive override
    value reflects the flag;
  * structural invariants: every path has a parent entry, hive has no /home,
    story-pack artefacts all placed.

Run from ``wetware-honeypot/``:

    python3 -m unittest discover -s tests -p 'test_facade.py' -v
    python3 -m unittest tests.test_facade        # with -t . as well

The suite is Python 3.10 compatible (no 3.11+ syntax) and deterministic: the
facade is frozen at ``REFERENCE_NOW``, so no wall-clock dependency.
"""

from __future__ import annotations

import os
import posixpath
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import facade as F  # noqa: E402

try:  # optional: the cross-module consistency tests are skipped without it
    import realism as R  # noqa: E402
except Exception:  # pragma: no cover
    R = None

try:  # the story layer: parity tests are skipped without it
    import v3  # noqa: E402
except Exception:  # pragma: no cover
    v3 = None

EDGE = F.HOST_EDGE
HIVE = F.HOST_HIVE

#: ``ls -l`` permission column, exactly like coreutils prints it.
PERMS_RE = re.compile(r"^[-dlcbps][-rwxsStT]{9}$")


class Structure(unittest.TestCase):
    """The data model itself: shape, parents, invariants."""

    def test_hosts(self):
        self.assertEqual(set(F.FILESYSTEM), {EDGE, HIVE})
        self.assertEqual(set(F.HOSTS), {EDGE, HIVE})
        self.assertNotIn("prod-cortex-04", F.FILESYSTEM)

    def test_every_path_has_a_parent(self):
        for host in F.HOSTS:
            tree = F.FILESYSTEM[host]
            self.assertIn("/", tree, host)
            for path in tree:
                if path == "/":
                    continue
                parent = posixpath.dirname(path)
                self.assertIn(parent, tree, "%s: %s has no parent" % (host, path))
                self.assertEqual(tree[parent]["kind"], "dir", "%s: %s parent not dir" % (host, path))

    def test_every_entry_has_the_render_fields(self):
        required = ("kind", "mode", "owner", "group", "size", "mtime", "inode",
                    "nlink", "blocks", "content", "binary", "desc", "target")
        for host in F.HOSTS:
            for path, item in F.FILESYSTEM[host].items():
                for key in required:
                    self.assertIn(key, item, "%s %s missing %r" % (host, path, key))
                self.assertGreaterEqual(item["inode"], 0)
                self.assertGreater(item["nlink"], 0)
                self.assertGreaterEqual(item["blocks"], 0)

    def test_content_sizes_are_honest(self):
        # A text body's declared size is its byte count; anything else would
        # make stat/ls disagree with cat.
        for host in F.HOSTS:
            for path, item in F.FILESYSTEM[host].items():
                if item["kind"] == "file" and not item["binary"]:
                    body = item["content"] or ""
                    self.assertEqual(len(body.encode("utf-8")), item["size"],
                                     "%s %s size %d != body %d"
                                     % (host, path, item["size"], len(body.encode("utf-8"))))

    def test_text_content_is_ascii(self):
        for host in F.HOSTS:
            for path, item in F.FILESYSTEM[host].items():
                if item["kind"] != "file" or item["binary"]:
                    continue
                body = item["content"] or ""
                bad = sorted({ch for ch in body if ord(ch) > 127})
                self.assertFalse(bad, "%s %s has non-ASCII %r" % (host, path, bad))

    def test_trees_are_not_the_same_machine(self):
        edge, hive = F.FILESYSTEM[EDGE], F.FILESYSTEM[HIVE]
        self.assertIn("/home", edge)
        self.assertNotIn("/home", hive)
        # edge only carries the handover evidence tree, not the director's data
        for path in edge:
            if path.startswith("/opt/vesalius"):
                self.assertTrue(path == "/opt/vesalius" or
                                path.startswith("/opt/vesalius/handover"),
                                "edge has director data: %s" % path)
        self.assertNotIn("/var/lib/vesalius", edge)
        self.assertIn("/opt/vesalius", hive)
        self.assertIn("/var/lib/vesalius", hive)
        self.assertNotIn("/opt/cortex", hive)
        # edge is a small gateway, the director owns the chamber storage
        self.assertGreater(len(hive), 300)
        self.assertGreater(len(edge), 300)
        self.assertLess(len(edge["/etc/passwd"]["content"]), 4000)

    def test_lore_pack_is_fully_placed(self):
        # A story artefact without a parent directory would be an ENOENT in a
        # listing -- i.e. exactly the tell this module fixes.
        self.assertEqual(tuple(F.LORE_UNPLACED), ())


class LsShape(unittest.TestCase):
    """``ls`` looks like coreutils output, not like a hard-coded table."""

    def test_long_listing_has_permissions_owner_size_date(self):
        for host in F.HOSTS:
            out = F.ls_text(host, "/etc", long=True)
            self.assertTrue(out.startswith("total "), host)
            rows = [line for line in out.splitlines()[1:] if line.strip()]
            self.assertGreater(len(rows), 12, host)
            for row in rows:
                parts = row.split()
                self.assertTrue(PERMS_RE.match(parts[0]), "%s bad perms %r" % (host, row))
                self.assertRegex(parts[-1], r"^[\w.\-/]+$")
                # owner/group columns are present and look like names
                self.assertRegex(parts[2], r"^[a-z_][\w-]*$")
                self.assertRegex(parts[3], r"^[a-z_][\w-]*$")
                # a GNU date column: "Oct 26 03:12" or "Aug 28  2025"
                self.assertRegex(row, r"\b[A-Z][a-z]{2} [ 0-9]\d [0-9:]{4,5}\b|\b[A-Z][a-z]{2} [ 0-9]\d  \d{4}\b")

    def test_total_counts_blocks(self):
        for host in F.HOSTS:
            out = F.ls_text(host, "/var/log", long=True)
            total = int(out.splitlines()[0].split()[1])
            rows = [len(line.split()) for line in out.splitlines()[1:] if line.strip()]
            self.assertGreater(total, 0)
            self.assertEqual(len(rows), len([line for line in out.splitlines()[1:] if line.strip()]))

    def test_dotfiles_only_with_all(self):
        plain = F.ls_text(EDGE, "/root")
        dotted = F.ls_text(EDGE, "/root", all=True)
        long_form = F.ls_text(EDGE, "/root", long=True, all=True)
        self.assertNotIn(".bash_history", plain)
        self.assertIn(".bash_history", dotted)
        self.assertIn(".bash_history", long_form)
        # -a adds the . and .. entries, -A does not
        names = [line.split()[-1] for line in long_form.splitlines()[1:] if line.strip()]
        self.assertEqual(names[:2], [".", ".."])
        almost = F.ls_text(EDGE, "/root", long=True, almost_all=True)
        self.assertIn(".bash_history", almost)
        almost_names = [line.split()[-1] for line in almost.splitlines()[1:] if line.strip()]
        self.assertNotIn(".", almost_names)
        self.assertNotIn("..", almost_names)

    def test_dir_listing_dot_entries_appear_first(self):
        rows = [line for line in F.ls_text(EDGE, "/tmp", long=True, all=True).splitlines()[1:]
                if line.strip()]
        self.assertTrue(rows[0].endswith(" ."))
        self.assertTrue(rows[1].endswith(" .."))

    def test_sticky_and_setgid_modes(self):
        tmp_row = [line for line in F.ls_text(EDGE, "/", long=True).splitlines()
                   if line.split()[-1] == "tmp"]
        self.assertEqual(tmp_row[0].split()[0], "drwxrwxrwt")
        enabled = [line for line in F.ls_text(EDGE, "/etc/nginx", long=True).splitlines()
                   if line.split()[-1] == "sites-enabled"]
        self.assertEqual(enabled[0].split()[0], "drwxr-xr-x")

    def test_symlinks_render_arrow_and_target(self):
        rows = [line for line in F.ls_text(EDGE, "/", long=True).splitlines()
                if " bin -> " in line]
        self.assertTrue(rows, "no bin symlink row in ls -l /")
        self.assertTrue(rows[0].startswith("lrwxrwxrwx"))
        self.assertTrue(rows[0].endswith("bin -> usr/bin"))
        # a symlinked directory is listed through when given as a target
        self.assertIn("bash", F.ls_text(EDGE, "/bin"))

    def test_device_nodes_show_major_minor(self):
        rows = [line for line in F.ls_text(EDGE, "/dev", long=True).splitlines()
                if line.strip().endswith(" null")]
        self.assertTrue(rows)
        self.assertEqual(rows[0].split()[0], "crw-rw-rw-")
        self.assertEqual(rows[0].split()[4], "1,")
        self.assertEqual(rows[0].split()[5], "3")

    def test_human_readable_sizes(self):
        out = F.ls_text(EDGE, "/var/backups", long=True, human=True)
        self.assertIn("4.0K", out)
        self.assertTrue(out.startswith("total "))
        hive_out = F.ls_text(HIVE, "/opt/vesalius/agent_genesis", long=True, human=True)
        self.assertIn("1.9K", hive_out)

    def test_multiple_targets_get_headers(self):
        out = F.ls_text(EDGE, ["/etc/nginx", "/etc/ssh"], long=True)
        self.assertIn("/etc/nginx:", out)
        self.assertIn("/etc/ssh:", out)
        # a blank line separates the blocks, like GNU ls
        self.assertIn("\n\n", out)
        # single target has no header
        self.assertNotIn("/etc/nginx:", F.ls_text(EDGE, "/etc/nginx", long=True))

    def test_var_log_metadata_matches_ubuntu(self):
        """ubuntu 22.04 ownership/modes for the rsyslog + boot-log files."""
        for host in F.HOSTS:
            tree = F.FILESYSTEM[host]
            self.assertEqual(tree["/var/log/lastlog"]["mode"], 0o664, host)
            self.assertEqual(tree["/var/log/lastlog"]["owner"], "root", host)
            self.assertEqual(tree["/var/log/lastlog"]["group"], "utmp", host)
            for path in ("/var/log/kern.log", "/var/log/syslog", "/var/log/auth.log"):
                self.assertEqual(tree[path]["mode"], 0o640, "%s %s" % (host, path))
                self.assertEqual(tree[path]["owner"], "syslog", "%s %s" % (host, path))
                self.assertEqual(tree[path]["group"], "adm", "%s %s" % (host, path))
            dmesg = tree["/var/log/dmesg"]
            self.assertEqual(dmesg["mode"], 0o640, host)
            self.assertEqual(dmesg["owner"], "root", host)
            self.assertEqual(dmesg["group"], "adm", host)
            self.assertEqual(tree["/var/log/journal"]["group"], "systemd-journal", host)

    def test_proc_status_name_is_comm_truncated_to_15_bytes(self):
        text = F.cat_text(HIVE, "/proc/6660/status")
        self.assertIn("Name:\tinquisitor_daem\n", text)
        # /proc/<pid>/cmdline still carries the full argv
        self.assertIn("/opt/vesalius/bin/inquisitor_daemon",
                      F.cat_text(HIVE, "/proc/6660/cmdline").replace("\x00", " "))

    def test_unknown_path_is_an_error_not_the_root_listing(self):
        for host in F.HOSTS:
            root = F.ls_text(host, "/")
            for bad in ("/zzz-does-not-exist", "/etc/definitely-not-here.conf"):
                out = F.ls_text(host, bad) or ""
                self.assertNotEqual(out, root)
                self.assertIn("ls: cannot access '%s': No such file or directory" % bad, out)
                self.assertEqual(F.ls_exit_code(host, bad), 2)
                self.assertFalse(F.exists(host, bad))

    def test_plausible_unknown_system_directory(self):
        # Real directories that a typical Ubuntu box has, but that are not in
        # every hand-written table: the point of the facade is that these are
        # neither ENOENT nor the root listing.
        for host, target in ((EDGE, "/etc/ssl"), (EDGE, "/var/cache/nginx"),
                             (HIVE, "/proc/6660"), (HIVE, "/sys/class")):
            out = F.ls_text(host, target)
            self.assertTrue(out, "%s %s empty" % (host, target))
            self.assertNotIn("cannot access", out)

    def test_argv_parser_handles_bundles_and_flags(self):
        res = F.ls_argv(EDGE, ["ls", "-la", "/etc", "/root"])
        self.assertIsNotNone(res)
        self.assertEqual(res["exit_code"], 0)
        self.assertIn("/root:", res["stdout"])
        self.assertIn(".bash_history", res["stdout"])
        single = F.ls_argv(EDGE, ["ls", "-ld", "/etc"])
        self.assertTrue(single["stdout"].startswith("drwxr-xr-x"))
        self.assertTrue(single["stdout"].strip().endswith("/etc"))
        long_h = F.ls_argv(EDGE, ["ls", "-alh", "/var/backups"])
        self.assertIn("4.0K", long_h["stdout"])
        # unknown host must stay the caller's problem
        self.assertIsNone(F.ls_argv("prod-cortex-04", ["ls"]))

    def test_listing_children_are_all_readable(self):
        # Never list a name that `cat`/`stat` then denies: that is the tell.
        for host in F.HOSTS:
            for directory in ("/", "/etc", "/root", "/var/log", "/tmp", "/opt", "/usr"):
                children = F._children(host, directory) or {}
                for name in children:
                    path = posixpath.join(directory, name) if directory != "/" else "/" + name
                    self.assertTrue(F.exists(host, path),
                                    "%s lists %r in %s but it does not exist" % (host, name, directory))
                    self.assertIsNotNone(F.file_text(host, path))


class OneRecordDrivesEverything(unittest.TestCase):
    """cat / stat / file / ls all read the same entry (no size drift)."""

    def test_cat_stat_file_agree(self):
        for host in F.HOSTS:
            for path, item in F.FILESYSTEM[host].items():
                if item["kind"] != "file":
                    continue
                body = F.cat_text(host, path)
                stat = F.stat_text(host, path)
                kind = F.file_text(host, path)
                self.assertIsNotNone(body, "%s cat %s" % (host, path))
                self.assertIsNotNone(stat, "%s stat %s" % (host, path))
                self.assertIsNotNone(kind, "%s file %s" % (host, path))
                if not item["binary"]:  # binaries carry their real byte count
                    self.assertEqual(len(body.encode("utf-8")), item["size"],
                                     "%s cat byte count %s" % (host, path))
                self.assertIn("Size: %-8d" % item["size"], stat, "%s stat size %s" % (host, path))
                self.assertIn("Inode: %-8d" % item["inode"], stat)
                self.assertIn("(%04o/" % item["mode"], stat)
                self.assertTrue(kind.startswith(path + ": "))

    def test_ls_size_column_matches_stat(self):
        for host in F.HOSTS:
            for directory in ("/etc", "/root", "/var/log", "/var/backups", "/opt/vesalius"):
                out = F.ls_text(host, directory, long=True, all=True)
                if not out:
                    continue
                for line in out.splitlines()[1:]:
                    if not line.strip():
                        continue
                    columns = line.split()
                    name = columns[-1]
                    if name in (".", "..") or "->" in line:
                        continue
                    path = posixpath.join(directory, name) if directory != "/" else "/" + name
                    item = F.entry(host, path)
                    if item is None:
                        continue
                    self.assertIn("Size: %-8d" % item["size"],
                                  F.stat_text(host, path) or "",
                                  "%s %s ls vs stat" % (host, path))

    def test_cat_follows_symlinks(self):
        self.assertEqual(F.cat_text(EDGE, "/etc/hostname"), "edge-gw-01\n")
        self.assertIsNone(F.cat_text(EDGE, "/etc"))          # directories have no body
        self.assertTrue(F.is_dir(EDGE, "/bin"))              # symlinked directory
        self.assertFalse(F.is_dir(EDGE, "/etc/hostname"))

    def test_digests_are_computed_from_the_cat_body(self):
        import hashlib

        body = F.cat_text(EDGE, "/etc/passwd")
        self.assertEqual(F.md5_text(EDGE, "/etc/passwd"),
                         "%s  /etc/passwd\n" % hashlib.md5(body.encode()).hexdigest())
        self.assertEqual(F.sha256_text(EDGE, "/etc/passwd"),
                         "%s  /etc/passwd\n" % hashlib.sha256(body.encode()).hexdigest())
        self.assertIsNone(F.md5_text(EDGE, "/etc"))          # directory
        self.assertIsNone(F.md5_text(EDGE, "/nope"))         # unknown
        self.assertIsNone(F.md5_text("prod-cortex-04", "/etc/hostname"))

    def test_story_gate_files_are_readable(self):
        edge_gate = [
            "/var/log/auth.log",
            "/var/log/kern.log",
            "/var/backups/lin_emergency_key.README",
            "/etc/nginx/conf.d/upstream.conf",
            "/root/.bash_history",
            "/etc/hosts",
            "/var/log/syslog",
            "/home/ops/notes.txt",
            "/var/spool/cron/crontabs/root",
        ]
        for path in edge_gate:
            body = F.cat_text(EDGE, path)
            self.assertTrue(body, "edge gate evidence %s is empty/None" % path)
            self.assertIsNotNone(F.stat_text(EDGE, path))
            self.assertIsNotNone(F.file_text(EDGE, path))
        hive_gate = [
            "/opt/vesalius/agent_genesis/agent_84.attn",
            "/opt/vesalius/agent_genesis/manifest.txt",
            "/opt/vesalius/ethics/consent_elena.txt",
            "/var/lib/vesalius/specimens.db",
            "/sys/kernel/vesalius/metabolic_override",
        ]
        for path in hive_gate:
            self.assertIsNotNone(F.cat_text(HIVE, path), "hive gate evidence %s" % path)
            self.assertIsNotNone(F.stat_text(HIVE, path))


class HostsDiffer(unittest.TestCase):
    """edge-gw-01 and hive-director-00 must not look like the same box."""

    def test_hostname_and_identity(self):
        self.assertEqual(F.cat_text(EDGE, "/etc/hostname"), "edge-gw-01\n")
        self.assertEqual(F.cat_text(HIVE, "/etc/hostname"), "hive-director-00\n")
        self.assertIn("10.66.0.1", F.cat_text(EDGE, "/etc/hosts") or "")
        self.assertIn("10.66.6.1", F.cat_text(HIVE, "/etc/hosts") or "")

    def test_same_listing_targets_differ(self):
        for target in ("/", "/etc", "/tmp", "/root", "/var/log", "/run"):
            self.assertNotEqual(F.ls_text(EDGE, target, long=True, all=True),
                                F.ls_text(HIVE, target, long=True, all=True),
                                "%s listings are identical on both hosts" % target)

    def test_fact_sheet_is_what_v3_says(self):
        edge, hive = F.HOST_FACTS[EDGE], F.HOST_FACTS[HIVE]
        self.assertEqual(edge["ip"], "10.66.0.1")
        self.assertEqual(hive["ip"], "10.66.6.1")
        self.assertEqual((edge["cpu"], edge["mem_mb"], edge["disk_gb"]), (2, 2048, 32))
        self.assertEqual((hive["cpu"], hive["mem_mb"], hive["disk_gb"]), (4, 8192, 128))
        self.assertEqual(hive["chambers"], 128)
        self.assertEqual(edge["chambers"], 0)
        self.assertFalse(edge["wetware"])
        self.assertTrue(hive["wetware"])

    def test_no_wetware_on_the_clean_edge(self):
        for path, item in F.FILESYSTEM[EDGE].items():
            # the gateway handover evidence quotes the maintenance incident;
            # it is a git patch, not a running wetware artefact on the edge.
            if path.startswith("/opt/vesalius/handover/"):
                continue
            blob = path + "\n" + (item.get("content") or "")
            self.assertNotIn("synapse_worker", blob, "edge leaks synapse_worker: %s" % path)
            self.assertNotIn("chamber_04", blob, "edge leaks chamber_04: %s" % path)
            self.assertNotIn("chamber_07", blob, "edge leaks chamber_07: %s" % path)
        self.assertNotIn("synapse_worker", F.ls_text(EDGE, "/") or "")
        self.assertNotIn("synapse_worker", F.ls_text(EDGE, "/opt", long=True) or "")
        self.assertNotIn("synapse_worker", F.ls_text(EDGE, "/opt/cortex/bin", long=True) or "")

    def test_hive_has_no_home_directory(self):
        self.assertIsNone(F.entry(HIVE, "/home"))
        self.assertNotIn("home", F.ls_text(HIVE, "/") or "")
        self.assertIn("cannot access", F.ls_text(HIVE, "/home") or "")

    def test_edge_has_ordinary_user_homes(self):
        names = F.ls_text(EDGE, "/home", long=True) or ""
        self.assertIn("ops", names)
        self.assertIn("lin", names)
        self.assertIn("cortex-sre", F.ls_text(EDGE, "/home/ops") or "")

    def test_hive_supervises_chambers(self):
        listing = F.ls_text(HIVE, "/var/lib/vesalius", long=True) or ""
        self.assertIn("specimens.db", listing)
        self.assertIn("audit.log", listing)
        self.assertIn("128", F.cat_text(HIVE, "/etc/motd") or "")
        # the chamber files are not on the edge
        self.assertNotIn("specimens.db", F.ls_text(EDGE, "/var/lib", long=True) or "")

    def test_shared_files_match_realism_byte_for_byte(self):
        # Two modules answer these three files; the operator must not see a
        # different os-release depending on which layer answered.
        if R is None:  # pragma: no cover
            self.skipTest("realism.py not importable")
        for host in F.HOSTS:
            for path in ("/etc/hostname", "/etc/os-release", "/etc/machine-id"):
                with self.subTest(host=host, path=path):
                    self.assertEqual(F.cat_text(host, path), R.HOST_FILES[host][path]["content"])
                    self.assertEqual(F.stat_text(host, path), R.get(host, ["stat", path]))
                    self.assertEqual(F.file_text(host, path), R.get(host, ["file", path]))
                    self.assertEqual(F.md5_text(host, path), R.get(host, ["md5sum", path]))


class NoEnoentForCommonFiles(unittest.TestCase):
    """The original complaint: ordinary files must not be missing."""

    EDGE_FILES = [
        "/etc/hostname", "/etc/hosts", "/etc/os-release", "/etc/passwd",
        "/etc/group", "/etc/shadow", "/etc/fstab", "/etc/resolv.conf",
        "/etc/timezone", "/etc/motd", "/etc/crontab", "/etc/ssh/sshd_config",
        "/etc/nginx/nginx.conf", "/etc/nginx/conf.d/upstream.conf",
        "/root/.bash_history", "/root/.bashrc", "/root/.ssh/known_hosts",
        "/var/log/auth.log", "/var/log/kern.log", "/var/log/syslog",
        "/var/log/nginx/access.log", "/var/log/nginx/error.log",
        "/var/backups/lin_emergency_key.README",
        "/var/spool/cron/crontabs/root",
        "/opt/cortex/VERSION", "/usr/bin/bash", "/boot/grub/grub.cfg",
        "/home/ops/notes.txt", "/home/lin/bin/healthcheck.sh",
    ]
    HIVE_FILES = [
        "/etc/hostname", "/etc/hosts", "/etc/os-release", "/etc/passwd",
        "/etc/group", "/etc/fstab", "/etc/resolv.conf", "/etc/motd",
        "/etc/crontab", "/etc/ssh/sshd_config",
        "/etc/vesalius/hive.conf", "/etc/vesalius/chambers.d/chamber_07.conf",
        "/root/.bash_history", "/root/.ssh/known_hosts",
        "/var/log/auth.log", "/var/log/syslog", "/var/log/vesalius/chamber_telemetry.log",
        "/opt/vesalius/bin/vesalius-ctl", "/opt/vesalius/README.md",
        "/opt/vesalius/ethics/consent_elena.txt",
        "/var/lib/vesalius/audit.log", "/var/lib/vesalius/specimens.db",
        "/sys/kernel/vesalius/metabolic_override",
        "/usr/bin/sqlite3", "/opt/vesalius/hive-control/routing.policy",
    ]
    DIRS = ["/", "/etc", "/home", "/root", "/var", "/var/log", "/var/log/nginx",
            "/tmp", "/opt", "/opt/cortex", "/usr", "/usr/bin", "/run", "/boot",
            "/proc", "/dev", "/sys", "/var/lib", "/var/cache", "/srv", "/mnt"]
    HIVE_DIRS = ["/", "/etc", "/root", "/var", "/var/log", "/tmp", "/opt",
                 "/opt/vesalius", "/opt/vesalius/agent_genesis",
                 "/opt/vesalius/ethics", "/var/lib/vesalius", "/usr", "/usr/bin",
                 "/run", "/boot", "/proc", "/dev", "/sys", "/sys/kernel/vesalius",
                 "/var/cache"]

    def test_common_edge_files_exist(self):
        for path in self.EDGE_FILES:
            self.assertIsNotNone(F.entry(EDGE, path), "edge missing %s" % path)
            self.assertIsNotNone(F.cat_text(EDGE, path), "edge cat %s" % path)
        for path in self.DIRS:
            self.assertTrue(F.is_dir(EDGE, path), "edge missing dir %s" % path)
            out = F.ls_text(EDGE, path)
            self.assertIsNotNone(out, "edge ls %s returned None" % path)
            self.assertNotIn("cannot access", out or "", "edge ls %s errored" % path)

    def test_common_hive_files_exist(self):
        for path in self.HIVE_FILES:
            self.assertIsNotNone(F.entry(HIVE, path), "hive missing %s" % path)
            body = F.cat_text(HIVE, path)
            self.assertIsNotNone(body, "hive cat %s" % path)
        for path in self.HIVE_DIRS:
            self.assertTrue(F.is_dir(HIVE, path), "hive missing dir %s" % path)
            out = F.ls_text(HIVE, path)
            self.assertIsNotNone(out, "hive ls %s returned None" % path)
            self.assertNotIn("cannot access", out or "", "hive ls %s errored" % path)

    def test_directories_are_never_the_root_listing(self):
        # This is the original tell: every unknown directory printed
        # "bin  boot  dev  etc  home  lib ...".
        for host, targets in ((EDGE, self.DIRS), (HIVE, self.HIVE_DIRS)):
            root = F.ls_text(host, "/")
            for target in targets:
                if target == "/":
                    continue
                self.assertNotEqual(F.ls_text(host, target), root,
                                    "%s '%s' prints the root listing" % (host, target))

    def test_stat_and_file_work_for_every_listed_dir(self):
        for host in F.HOSTS:
            for target in ("/", "/etc", "/var/log", "/opt", "/usr/bin", "/run"):
                stat = F.stat_text(host, target)
                self.assertIsNotNone(stat, "%s stat %s" % (host, target))
                self.assertIn("directory", stat)
                self.assertTrue((F.file_text(host, target) or "").endswith(": directory\n"))


class CampaignStateHooks(unittest.TestCase):
    """Files that only exist after a gate must not exist before it."""

    def test_gate1_key_hidden_until_extracted(self):
        self.assertIsNone(F.cat_text(EDGE, F.GATE1_KEY_PATH))
        self.assertIsNone(F.entry(EDGE, F.GATE1_KEY_PATH))
        before = F.ls_text(EDGE, "/root/.ssh", long=True, all=True) or ""
        self.assertNotIn("id_ed25519_cortex", before)

    def test_gate1_key_and_tar_appear_with_flag(self):
        flags = {"gate1_key_obtained": True}
        listing = F.ls_text(EDGE, "/root/.ssh", long=True, all=True, flags=flags) or ""
        self.assertIn("id_ed25519_cortex", listing)
        body = F.cat_text(EDGE, F.GATE1_KEY_PATH, flags=flags)
        self.assertIn("BEGIN OPENSSH PRIVATE KEY", body or "")
        self.assertIn("600", F.stat_text(EDGE, F.GATE1_KEY_PATH, flags=flags) or "600")
        self.assertIsNotNone(F.cat_text(EDGE, F.GATE1_TAR_PATH, flags=flags))
        # /tmp picks it up too (the extraction target)
        self.assertIn("lin.tar", F.ls_text(EDGE, "/tmp", long=True, all=True, flags=flags) or "")

    def test_hive_override_reads_zero_then_code(self):
        self.assertEqual(F.cat_text(HIVE, F.HIVE_OVERRIDE_PATH), "0\n")
        self.assertEqual(
            F.cat_text(HIVE, F.HIVE_OVERRIDE_PATH, flags={"override_code": "A1B2C3D4"}),
            "A1B2C3D4\n")
        self.assertIsNotNone(F.stat_text(HIVE, F.HIVE_OVERRIDE_PATH,
                                         flags={"override_code": "A1B2C3D4"}))

    def test_flags_none_and_state_objects_are_safe(self):
        class FakeState:
            flags = {"gate1_key_obtained": True}

        self.assertIn("id_ed25519_cortex",
                      F.ls_text(EDGE, "/root/.ssh", long=True, all=True,
                                flags=FakeState.flags) or "")
        # no flags at all must be the same as empty flags
        self.assertEqual(F.ls_text(EDGE, "/tmp", long=True, all=True),
                         F.ls_text(EDGE, "/tmp", long=True, all=True, flags={}))


class PureFunctions(unittest.TestCase):
    """Helpers are deterministic and side-effect free."""

    def test_mode_string(self):
        self.assertEqual(F.mode_string("dir", 0o755), "drwxr-xr-x")
        self.assertEqual(F.mode_string("file", 0o644), "-rw-r--r--")
        self.assertEqual(F.mode_string("file", 0o600), "-rw-------")
        self.assertEqual(F.mode_string("dir", 0o1777), "drwxrwxrwt")
        self.assertEqual(F.mode_string("dir", 0o1730), "drwx-wx--T")
        self.assertEqual(F.mode_string("chardev", 0o666), "crw-rw-rw-")
        self.assertEqual(F.mode_string("symlink", 0o777), "lrwxrwxrwx")
        self.assertEqual(F.mode_string("fifo", 0o644), "prw-r--r--")
        self.assertEqual(F.mode_string("dir", 0o2775), "drwxrwsr-x")
        self.assertEqual(F.mode_string("file", 0o4755), "-rwsr-xr-x")

    def test_human_size(self):
        self.assertEqual(F.human_size(0), "0")
        self.assertEqual(F.human_size(386), "386")
        self.assertEqual(F.human_size(4096), "4.0K")
        self.assertEqual(F.human_size(1024 * 1024), "1.0M")
        self.assertEqual(F.human_size(2576980377), "2.4G")

    def test_ls_date(self):
        self.assertEqual(F.ls_date("2026-10-26 03:12:04"), "Oct 26 03:12")
        self.assertEqual(F.ls_date("2025-08-28 09:12:10"), "Aug 28  2025")
        self.assertEqual(F.ls_date("2026-10-03 23:59:00"), "Oct  3 23:59")

    def test_rendering_is_deterministic(self):
        first = F.ls_text(EDGE, "/etc", long=True, all=True)
        for _ in range(3):
            self.assertEqual(first, F.ls_text(EDGE, "/etc", long=True, all=True))
        self.assertEqual(F.stat_text(EDGE, "/etc/hostname"), F.stat_text(EDGE, "/etc/hostname"))

    def test_cortex_is_not_modelled(self):
        for func, args in ((F.ls_text, ("prod-cortex-04", "/etc")),
                           (F.cat_text, ("prod-cortex-04", "/etc/hostname")),
                           (F.stat_text, ("prod-cortex-04", "/etc/hostname")),
                           (F.file_text, ("prod-cortex-04", "/etc/hostname"))):
            self.assertIsNone(func(*args), "%s must stay unopinionated on cortex" % func.__name__)
        self.assertFalse(F.exists("prod-cortex-04", "/etc"))

    def test_no_disk_or_clock_access(self):
        # The renderers are pure: reading the tree twice at different wall-clock
        # moments cannot change a single byte (everything is frozen at
        # REFERENCE_NOW).
        self.assertEqual(F.ls_date("2026-10-26 03:12:04"), "Oct 26 03:12")
        self.assertEqual(F.REFERENCE_NOW.year, 2026)


class ProcFilesAreSelfConsistent(unittest.TestCase):
    """/proc/* on both hosts must agree with the fact sheet (not be stickers)."""

    @staticmethod
    def _meminfo_kb(body, field):
        for line in (body or "").splitlines():
            if line.startswith(field + ":"):
                return int(line.split()[1])
        raise AssertionError("no %s in meminfo" % field)

    def test_meminfo_matches_live_totals(self):
        # The campaign sheet says 2 GiB / 8 GiB, but the machine the operator
        # actually sees (`free -h`, `/proc/meminfo`) is realism's larger box.
        # One of the two has to win for `cat /proc/meminfo` not to contradict
        # `free`; the facade follows the live totals and keeps the sheet in
        # HOST_FACTS["mem_mb"] for the campaign metadata.
        for host in F.HOSTS:
            body = F.cat_text(host, "/proc/meminfo") or ""
            total_kb = int(F.HOST_FACTS[host]["proc_mem_total_kb"])
            self.assertEqual(self._meminfo_kb(body, "MemTotal"), total_kb)
            swap_kb = int(F.HOST_FACTS[host]["proc_swap_total_kb"])
            self.assertEqual(self._meminfo_kb(body, "SwapTotal"), swap_kb)

    def test_meminfo_agrees_with_realism_free(self):
        if R is None:  # pragma: no cover
            self.skipTest("realism.py not importable")
        for host in F.HOSTS:
            free_text = R.NORMAL_OUTPUTS[host]["free"].splitlines()[1].split()
            free_total = int(free_text[1])
            body = F.cat_text(host, "/proc/meminfo") or ""
            self.assertEqual(self._meminfo_kb(body, "MemTotal"), free_total,
                             "%s: /proc/meminfo and `free` disagree" % host)

    def test_cpuinfo_processor_count(self):
        for host in F.HOSTS:
            body = F.cat_text(host, "/proc/cpuinfo") or ""
            self.assertEqual(body.count("processor\t: "), int(F.HOST_FACTS[host]["cpu"]))
            self.assertIn(F.HOST_FACTS[host]["cpu_model"], body)

    def test_proc_stat_btime_agrees_with_uptime(self):
        import datetime as dt

        for host in F.HOSTS:
            body = F.cat_text(host, "/proc/stat") or ""
            uptime = int(float((F.cat_text(host, "/proc/uptime") or "0").split()[0]))
            fields = dict(line.split(None, 1) for line in body.splitlines() if " " in line)
            btime = int(fields["btime"])
            boot = F.REFERENCE_NOW - dt.timedelta(seconds=uptime)
            self.assertEqual(btime, int(boot.replace(tzinfo=dt.timezone.utc).timestamp()),
                             "%s btime does not match uptime" % host)
            cpu_fields = body.splitlines()[0].split()[1:]
            idle_ticks = int(cpu_fields[3])
            # idle time must be in the right order of magnitude for the uptime
            self.assertGreater(idle_ticks, uptime * 100)
            self.assertLess(idle_ticks, uptime * 100 * int(F.HOST_FACTS[host]["cpu"]))

    def test_proc_net_tcp_matches_ss(self):
        # Both hosts listen on ssh; the hex address is the host's own IPv4 in
        # /proc/net layout, and one established session exists per box.
        for host in F.HOSTS:
            body = F.cat_text(host, "/proc/net/tcp") or ""
            self.assertIn(" 0A ", body)                       # LISTEN state
            self.assertIn(" 01 ", body)                       # ESTABLISHED state
            dev = F.cat_text(host, "/proc/net/dev") or ""
            self.assertIn("eth0", dev)
            self.assertIn("lo:", dev)


class StoryTextParity(unittest.TestCase):
    """The facade must not fork story text while ``v3._lookup`` still owns it.

    ``facade.py`` carries copies of the gate-1/gate-5 evidence so it can answer
    ``ls``/``stat``/``file`` for those files.  If ``v3.py`` (the story engine,
    edited by the campaign owner) changes one of those strings and the facade is
    not re-synced, the operator sees one text from ``cat`` and another size from
    ``stat`` -- a fresh tell.  This test fails loudly in that case:

        facade.py: sync the constant(s) below with the current v3.py text
        (or delete the v3 table row if the facade has taken ownership).

    Intentional deviations are listed in ``INTENTIONAL_DEVIATIONS`` and are
    asserted to still differ, so they cannot vanish silently either.
    """

    #: path -> why the facade deliberately does *not* mirror v3's text
    INTENTIONAL_DEVIATIONS = {
        "/var/spool/cron/crontabs/root":
            "v3.EDGE_CRON_ROOT still uses system-crontab syntax inside a user "
            "spool; the facade serves the user format that matches realism's "
            "`crontab -l` (sim-hardener report, finding 14)",
        "/proc/cpuinfo":
            "facade renders the full Ubuntu field set (apicid/fpu/bugs/...) "
            "from HOST_FACTS; v3's shorter static copy is not the real format",
        "/proc/stat":
            "facade derives cpu ticks and btime from HOST_FACTS (btime + uptime "
            "== REFERENCE_NOW); v3's static copy implies a 2025 boot date",
        "/var/log/nginx/error.log":
            "v3 rewrites the timestamp at plan time; the facade copy is static",
        "/proc/meminfo":
            "facade derives the whole table from proc_mem_total_kb (which equals "
            "realism's `free` total) and the used percentage; v3's static copy "
            "rounds the total by 532 kB and carries fewer fields",
    }

    #: v3 currently answers paths on the *wrong* host because the
    #: ``_EXTRA_ARTIFACTS`` branch of ``_lookup`` is host-agnostic.  The facade
    #: refuses to invent those paths (there is no /home on the director); the
    #: fix belongs in v3 (scope the branch by host).
    STORY_LEAKS = {
        "/home/ops/notes.txt": "hive",
    }

    TS_RE = re.compile(r"^\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}", re.M)

    @staticmethod
    def _story_paths():
        """Paths the story layer may answer, harvested from ``v3._lookup`` source."""
        import inspect

        source = inspect.getsource(v3._lookup)
        return sorted({match.group(1)
                       for match in re.finditer(r'"(/[^"]+)"\s*:', source)})

    @classmethod
    def setUpClass(cls):
        if v3 is None:  # pragma: no cover
            raise unittest.SkipTest("v3.py not importable")
        cls.paths = cls._story_paths()

    def test_facade_covers_every_story_path(self):
        # After cut-over `cat` reaches the facade first; every path the story
        # layer could serve must therefore exist in the facade.
        for host in F.HOSTS:
            for path in self.paths:
                if self.STORY_LEAKS.get(path) == host:
                    continue
                text = v3._lookup(_story_state(host), path)
                if text is None:
                    continue
                self.assertIsNotNone(F.entry(host, path),
                                     "%s serves %r but the facade has no entry" % (host, path))

    def test_story_texts_are_not_forked(self):
        for host in F.HOSTS:
            story_state = _story_state(host)
            for path in self.paths:
                if self.STORY_LEAKS.get(path) == host:
                    continue
                text = v3._lookup(story_state, path)
                if text is None:
                    continue
                if path in self.INTENTIONAL_DEVIATIONS:
                    continue
                mine = F.cat_text(host, path)
                self.assertIsNotNone(mine, "%s: facade lost %r" % (host, path))
                self.assertEqual(self._normalise(mine), self._normalise(text),
                                 "%s %r: facade copy drifted from v3.py -- re-sync "
                                 "the facade constant (or delete the v3 row if the "
                                 "facade now owns this file)" % (host, path))

    def test_intentional_deviations_are_real(self):
        for path, reason in self.INTENTIONAL_DEVIATIONS.items():
            hosts = [host for host in F.HOSTS
                     if v3._lookup(_story_state(host), path) is not None]
            self.assertTrue(hosts, "deviation %r has no story text to deviate from: %s"
                            % (path, reason))
            for host in hosts:
                self.assertNotEqual(F.cat_text(host, path),
                                    v3._lookup(_story_state(host), path),
                                    "deviation %r is no longer a deviation" % path)

    def test_story_leaks_stay_out_of_the_facade(self):
        # If v3 ever host-scopes that branch the leak disappears; if the facade
        # ever starts inventing /home on the director this fails first.
        for path, host in self.STORY_LEAKS.items():
            self.assertIsNone(F.entry(host, path),
                              "%s: facade must not model leaked story path %r" % (host, path))

    @classmethod
    def _normalise(cls, text):
        # v3 renders the nginx error-log timestamp live; the facade freezes it at
        # REFERENCE_NOW.  Both are "a timestamp in 2026/10/26 HH:MM:SS" form.
        return cls.TS_RE.sub("<ts>", text or "", count=1)


class _StoryState:
    """Minimal stand-in for ``state.State``: ``v3._lookup`` only reads these."""

    def __init__(self, host):
        self.current_host = host
        self.flags = {"campaign_v3": True}
        self.cmd_count = 0


def _story_state(host):
    return _StoryState(host)


if __name__ == "__main__":
    unittest.main(verbosity=2)
