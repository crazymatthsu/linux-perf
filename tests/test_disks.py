"""Disk space: folder size (du -sx), filesystem usage (df), config, recording.

Run from the repository root:  python3 -m unittest discover -s tests -v
"""

import csv
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from perfmon import disks, report  # noqa: E402
from perfmon.config import Config  # noqa: E402
from perfmon.recorder import Recorder  # noqa: E402


def make_tree(root):
    os.makedirs(os.path.join(root, "app1", "archive"))
    with open(os.path.join(root, "app1", "server.log"), "wb") as f:
        f.write(os.urandom(300000))
    with open(os.path.join(root, "app1", "archive", "server.log.1"), "wb") as f:
        f.write(os.urandom(120000))
    with open(os.path.join(root, "app1", "sparse.dat"), "wb") as f:
        f.seek(50 * 1024 * 1024)          # 50 MB apparent size, almost nothing allocated
        f.write(b"x")
    os.link(os.path.join(root, "app1", "server.log"), os.path.join(root, "app1", "server.hardlink"))
    os.symlink("/usr", os.path.join(root, "app1", "usr-link"))   # must not be followed


def gnu_du_bytes(path):
    try:
        out = subprocess.check_output(["du", "-sxB1", path], stderr=subprocess.DEVNULL)
        return int(out.split()[0])
    except (OSError, subprocess.CalledProcessError, ValueError):
        return None


class DirUsageTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        make_tree(self.dir)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_matches_du(self):
        r = disks.dir_usage(self.dir)
        self.assertTrue(r["complete"])
        self.assertEqual(r["files"], 4)                 # hard link counted once, symlink is a file
        self.assertLess(r["bytes"], 5 * 1024 * 1024)   # sparse file counted by allocation
        expected = gnu_du_bytes(self.dir)
        if expected is not None:
            self.assertEqual(r["bytes"], expected)

    def test_deadline_returns_partial(self):
        r = disks.dir_usage(self.dir, deadline=time.monotonic() - 1)
        self.assertFalse(r["complete"])

    def test_missing_folder_raises(self):
        with self.assertRaises(OSError):
            disks.dir_usage(os.path.join(self.dir, "nope"))

    @unittest.skipIf(os.geteuid() == 0, "root can read everything")
    def test_unreadable_subdir_is_counted(self):
        locked = os.path.join(self.dir, "locked")
        os.makedirs(locked)
        os.chmod(locked, 0)
        try:
            self.assertEqual(disks.dir_usage(self.dir)["unreadable"], 1)
        finally:
            os.chmod(locked, 0o755)


class FsUsageTest(unittest.TestCase):
    def test_matches_statvfs(self):
        st = os.statvfs("/")
        fs = disks.fs_usage("/")
        self.assertEqual(fs["size"], st.f_blocks * st.f_frsize)
        self.assertEqual(fs["avail"], st.f_bavail * st.f_frsize)
        self.assertGreater(fs["used_pct"], 0)
        self.assertLessEqual(fs["used_pct"], 100)

    def test_mount_of(self):
        self.assertEqual(disks.mount_of("/"), "/")
        self.assertTrue(disks.mount_of(tempfile.gettempdir()).startswith("/"))


class ConfigTest(unittest.TestCase):
    def write(self, text):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        path = os.path.join(d, "perfmon.conf")
        with open(path, "w") as f:
            f.write(text)
        return path

    def test_defaults(self):
        cfg = Config(None)
        self.assertEqual(cfg.disk_paths, ["/logs", "/apps"])
        self.assertEqual(cfg.disk_scan_interval, 60)

    def test_custom_and_off(self):
        cfg = Config(self.write("[perfmon]\ndisk_paths = /data/logs, /opt/app ,/data/logs\n"
                                "disk_scan_interval = 0\n"))
        self.assertEqual(cfg.disk_paths, ["/data/logs", "/opt/app"])
        self.assertEqual(cfg.disk_scan_interval, 0)
        self.assertEqual(Config(self.write("[perfmon]\ndisk_paths =\n")).disk_paths, [])

    def test_multiline(self):
        cfg = Config(self.write("[perfmon]\ndisk_paths =\n    /logs\n    /apps\n    /var/log\n"))
        self.assertEqual(cfg.disk_paths, ["/logs", "/apps", "/var/log"])


class RecordingTest(unittest.TestCase):
    def test_disks_csv(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        watched = os.path.join(d, "logs")
        os.makedirs(watched)
        make_tree(watched)
        missing = os.path.join(d, "apps")
        conf = os.path.join(d, "perfmon.conf")
        with open(conf, "w") as f:
            f.write("[perfmon]\ninterval = 0.5\noutput_dir = data\ndocker_cmd = false\n"
                    "disk_paths = %s, %s\ndisk_scan_interval = 1\n" % (watched, missing))
        run = Recorder(Config(conf), name="disk").run(duration=2.5)
        with open(os.path.join(run, "disks.csv"), newline="") as f:
            rows = list(csv.DictReader(f))
        self.assertTrue(rows)
        self.assertEqual({r["path"] for r in rows}, {watched})       # missing folder skipped
        last = rows[-1]
        self.assertGreater(float(last["fs_size_mb"]), 0)
        self.assertTrue(last["mount"].startswith("/"))
        self.assertEqual(last["dir_files"], "4")
        self.assertAlmostEqual(float(last["dir_size_mb"]) * 1048576,
                               disks.dir_usage(watched)["bytes"], delta=1048576 * 0.01)
        s = report.summarize(run)
        self.assertEqual([e["path"] for e in s["disks"]], [watched])


if __name__ == "__main__":
    unittest.main()
