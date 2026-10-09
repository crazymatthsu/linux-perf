"""Unit tests for the /proc, cgroup and hsperfdata readers.

Run from the repository root:  python3 -m unittest discover -s tests -v
"""

import os
import shutil
import struct
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from perfmon import cgroups, hsperf, procfs  # noqa: E402


class JavaNameTest(unittest.TestCase):
    def test_main_class(self):
        argv = ["/opt/java/bin/java", "-Xmx4g", "-cp", "/app/lib/*", "-Dfoo=bar",
                "io.deephaven.server.jetty.JettyMain", "--port", "10000"]
        self.assertEqual(procfs.java_main(argv), "JettyMain")
        self.assertEqual(procfs.process_name("java", argv), "java:JettyMain")

    def test_jar_and_module(self):
        self.assertEqual(procfs.java_main(["java", "-Xms1g", "-jar", "/srv/orders-service-1.2.jar"]),
                         "orders-service-1.2.jar")
        self.assertEqual(procfs.java_main(["java", "--add-opens", "java.base/java.lang=ALL-UNNAMED",
                                           "-m", "com.acme.risk/com.acme.risk.Main"]), "com.acme.risk.Main")
        self.assertEqual(procfs.java_main(["java", "@/app/jvm.args", "com.acme.Pricer"]), "Pricer")
        self.assertIsNone(procfs.java_main(["java", "-version"]))

    def test_non_java(self):
        self.assertEqual(procfs.process_name("ampServer", ["/AMPS/bin/ampServer", "config.xml"]), "ampServer")
        self.assertEqual(procfs.process_name("python3", ["python3", "-u", "/opt/feed.py"]), "python3:feed.py")
        self.assertEqual(procfs.process_name("kworker", []), "kworker")


class FakeProcTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.old = procfs.PROC
        procfs.PROC = self.dir
        os.makedirs(os.path.join(self.dir, "42"))

    def tearDown(self):
        procfs.PROC = self.old
        shutil.rmtree(self.dir)

    def write(self, name, text):
        with open(os.path.join(self.dir, name), "w") as f:
            f.write(textwrap.dedent(text))

    def test_stat_with_spaces_and_parens_in_comm(self):
        fields = ["S", "1", "42", "42", "0", "-1", "4194560", "100", "0", "7", "0", "250", "50",
                  "0", "0", "20", "0", "33", "0", "123456", "1048576000", "25600"]
        self.write("42/stat", "42 (my (weird) proc) " + " ".join(fields) + "\n")
        st = procfs.read_stat(42)
        self.assertEqual(st["comm"], "my (weird) proc")
        self.assertEqual((st["utime"], st["stime"], st["threads"]), (250, 50, 33))
        self.assertEqual(st["starttime"], 123456)
        self.assertEqual(st["rss"], 25600 * procfs.PAGE_SIZE)

    def test_status_nspid(self):
        self.write("42/status", """\
            Name:\tjava
            VmRSS:\t  204800 kB
            VmSwap:\t    1024 kB
            NSpid:\t42\t1
            """)
        st = procfs.read_status(42)
        self.assertEqual(st["NSpid"], 1)
        self.assertEqual(st["VmSwap"], 1024 * 1024)

    def test_host_cpu_and_net(self):
        self.write("stat", "cpu  100 5 50 800 20 0 5 10 0 0\ncpu0 1 2 3 4\nctxt 999\n")
        cpu = procfs.read_host_cpu()
        self.assertEqual(cpu["total"], 990)
        self.assertEqual((cpu["idle"], cpu["iowait"], cpu["steal"], cpu["ctxt"]), (800, 20, 10, 999))
        os.makedirs(os.path.join(self.dir, "self", "net"))
        self.write("self/net/dev", """\
            Inter-|   Receive                                                |  Transmit
             face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed
                lo: 5000 10 0 0 0 0 0 0 5000 10 0 0 0 0 0 0
              eth0: 1000 10 0 0 0 0 0 0 2000 10 0 0 0 0 0 0
           docker0: 7000 10 0 0 0 0 0 0 7000 10 0 0 0 0 0 0
            """)
        self.assertEqual(procfs.read_net_dev(None, physical_only=True)[:2], (1000, 2000))
        self.assertEqual(procfs.read_net_dev(None)[:2], (8000, 9000))

    def test_container_id(self):
        cid = "ab" * 32
        text = "0::/system.slice/docker-%s.scope\n" % cid
        self.assertEqual(procfs.container_ids_in(text), [cid])
        self.assertEqual(cgroups._cut_after("/system.slice/docker-%s.scope/init.scope" % cid, cid),
                         "/system.slice/docker-%s.scope" % cid)


class CgroupReadTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir)

    def write(self, rel, text):
        path = os.path.join(self.dir, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(textwrap.dedent(text))

    def test_v2(self):
        self.write("c/cpu.stat", "usage_usec 5000000\nuser_usec 4000000\nsystem_usec 1000000\n"
                                 "nr_periods 100\nnr_throttled 25\nthrottled_usec 900\n")
        self.write("c/cpu.max", "150000 100000\n")
        self.write("c/memory.current", "524288000\n")
        self.write("c/memory.max", "max\n")
        self.write("c/memory.stat", "anon 400000000\nfile 120000000\ninactive_file 100000000\n")
        self.write("c/io.stat", "8:0 rbytes=1000 wbytes=2000 rios=1 wios=2\n8:16 rbytes=10 wbytes=20\n")
        self.write("c/pids.current", "33\n")
        v = cgroups.Cgroups._read_v2(os.path.join(self.dir, "c"))
        self.assertEqual(v["cpu_ns"], 5000000000)
        self.assertEqual((v["periods"], v["throttled"]), (100, 25))
        self.assertAlmostEqual(v["cpu_limit"], 1.5)
        self.assertIsNone(v["mem_limit"])
        self.assertEqual(v["mem_usage"] - v["mem_inactive_file"], 424288000)
        self.assertEqual((v["io_read"], v["io_write"], v["pids"]), (1010, 2020, 33))

    def test_v1(self):
        self.write("cpuacct/d/cpuacct.usage", "7000000000\n")
        self.write("cpu/d/cpu.stat", "nr_periods 10\nnr_throttled 2\nthrottled_time 5000\n")
        self.write("cpu/d/cpu.cfs_quota_us", "50000\n")
        self.write("cpu/d/cpu.cfs_period_us", "100000\n")
        self.write("memory/d/memory.usage_in_bytes", "300000000\n")
        self.write("memory/d/memory.limit_in_bytes", "9223372036854771712\n")
        self.write("memory/d/memory.stat", "cache 5\nrss 6\ntotal_cache 50000000\ntotal_inactive_file 40000000\n")
        self.write("blkio/d/blkio.throttle.io_service_bytes", "8:0 Read 4096\n8:0 Write 8192\n8:0 Total 12288\nTotal 12288\n")
        d = lambda c: os.path.join(self.dir, c, "d")  # noqa: E731
        v = cgroups.Cgroups._read_v1({"cpu": d("cpu"), "cpuacct": d("cpuacct"),
                                      "memory": d("memory"), "blkio": d("blkio")})
        self.assertEqual(v["cpu_ns"], 7000000000)
        self.assertAlmostEqual(v["cpu_limit"], 0.5)
        self.assertIsNone(v["mem_limit"])               # "unlimited" sentinel
        self.assertEqual(v["mem_inactive_file"], 40000000)
        self.assertEqual((v["io_read"], v["io_write"]), (4096, 8192))


class CgroupV2LayoutTest(unittest.TestCase):
    """RHEL 9/10 style hosts: pure cgroup v2, systemd cgroup driver."""

    CID = "0123456789abcdef" * 4

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.old = procfs.PROC
        procfs.PROC = os.path.join(self.dir, "proc")
        self.cgroot = os.path.join(self.dir, "sys", "fs", "cgroup")
        os.makedirs(os.path.join(procfs.PROC, "self"))
        os.makedirs(self.cgroot)
        self.write("proc/self/mounts",
                   "proc /proc proc rw,nosuid,nodev,noexec,relatime 0 0\n"
                   "cgroup2 %s cgroup2 rw,seclabel,nosuid,nodev,noexec,relatime,nsdelegate,memory_recursiveprot 0 0\n"
                   % self.cgroot)
        self.write("sys/fs/cgroup/cgroup.controllers", "cpuset cpu io memory hugetlb pids rdma misc\n")

    def tearDown(self):
        procfs.PROC = self.old
        shutil.rmtree(self.dir)

    def write(self, rel, text):
        path = os.path.join(self.dir, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)

    def container_cgroup(self, rel, usage_usec, mem):
        d = "sys/fs/cgroup" + rel
        self.write(d + "/cpu.stat", "usage_usec %d\nnr_periods 10\nnr_throttled 0\n" % usage_usec)
        self.write(d + "/memory.current", "%d\n" % mem)
        self.write(d + "/memory.max", "max\n")
        self.write(d + "/memory.stat", "file 0\ninactive_file 0\n")

    def resolve(self, pid, cgroup_line):
        self.write("proc/%d/cgroup" % pid, cgroup_line + "\n")
        cg = cgroups.Cgroups()
        self.assertEqual(cg.version, "v2")
        ids = procfs.container_ids_in(procfs.read_cgroup(pid))
        dirs = cg.container_dirs(pid, ids[0]) if ids else {}
        return ids, dirs, cg

    def test_docker_systemd_driver(self):
        rel = "/system.slice/docker-%s.scope" % self.CID
        self.container_cgroup(rel, 3000000, 500 << 20)
        ids, dirs, cg = self.resolve(100, "0::" + rel)
        self.assertEqual(ids, [self.CID])
        self.assertEqual(dirs, {"unified": self.cgroot + rel})
        v = cg.read(dirs)
        self.assertEqual((v["cpu_ns"], v["mem_usage"]), (3000000000, 500 << 20))

    def test_podman_rootful_ignores_conmon(self):
        scope = "/machine.slice/libpod-%s.scope" % self.CID
        self.container_cgroup(scope, 9000000, 700 << 20)
        self.container_cgroup("/machine.slice/libpod-conmon-%s.scope" % self.CID, 1000, 1 << 20)
        # conmon is not part of the container ...
        ids, _, _ = self.resolve(200, "0::/machine.slice/libpod-conmon-%s.scope" % self.CID)
        self.assertEqual(ids, [])
        # ... the workload (in crun's "container" child cgroup) is, and is
        # measured at the libpod scope, which aggregates its children.
        ids, dirs, cg = self.resolve(201, "0::%s/container" % scope)
        self.assertEqual(ids, [self.CID])
        self.assertEqual(dirs, {"unified": self.cgroot + scope})
        self.assertEqual(cg.read(dirs)["cpu_ns"], 9000000000)

    def test_podman_rootless(self):
        scope = "/user.slice/user-1000.slice/user@1000.service/user.slice/libpod-%s.scope" % self.CID
        self.container_cgroup(scope, 5000, 64 << 20)
        ids, dirs, cg = self.resolve(300, "0::%s/container" % scope)
        self.assertEqual(ids, [self.CID])
        self.assertEqual(cg.read(dirs)["mem_usage"], 64 << 20)


def make_hsperf(counters, little=True):
    """Build a minimal version-2 hsperfdata image."""
    e = "<" if little else ">"
    entries = b""
    for name, value in counters.items():
        nb = name.encode() + b"\0"
        nb += b"\0" * ((-len(nb)) % 8)
        name_off = 20
        data_off = name_off + len(nb)
        length = data_off + 8
        entries += struct.pack(e + "iiibbbbi", length, name_off, 0, ord("J"), 0, 0, 0, data_off)
        entries += nb + struct.pack(e + "q", value)
    header = struct.pack(">I", 0xCAFEC0C0) + bytes([1 if little else 0, 2, 0, 1])
    header += struct.pack(e + "iiqii", 32 + len(entries), 0, 12345, 32, len(counters))
    return header + entries


class HsperfTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir)

    def counters_g1(self):
        mb = 1 << 20
        return {
            "sun.os.hrt.frequency": 1000000000,
            "java.threads.live": 42,
            "sun.gc.generation.0.capacity": 300 * mb,
            "sun.gc.generation.0.maxCapacity": 1024 * mb,
            "sun.gc.generation.0.space.0.used": 100 * mb,
            "sun.gc.generation.0.space.2.used": 10 * mb,
            "sun.gc.generation.1.capacity": 200 * mb,
            "sun.gc.generation.1.maxCapacity": 1024 * mb,
            "sun.gc.generation.1.space.0.used": 150 * mb,
            "sun.gc.metaspace.used": 60 * mb,
            "sun.gc.collector.0.invocations": 10,
            "sun.gc.collector.0.time": 250000000,
            "sun.gc.collector.1.invocations": 1,
            "sun.gc.collector.1.time": 1000000000,
            "sun.gc.collector.2.invocations": 4,
            "sun.gc.collector.2.time": 5000000,
            "sun.rt.unrelated": 7,
        }

    def check(self, little):
        path = os.path.join(self.dir, "1")
        with open(path, "wb") as f:
            f.write(make_hsperf(self.counters_g1(), little))
        s = hsperf.summarize(hsperf.PerfData(path).read())
        mb = 1 << 20
        self.assertEqual(s["heap_used"], 260 * mb)
        self.assertEqual(s["young_used"], 110 * mb)
        self.assertEqual(s["old_used"], 150 * mb)
        self.assertEqual(s["heap_committed"], 500 * mb)
        self.assertEqual(s["heap_max"], 1024 * mb)      # G1: not double-counted
        self.assertEqual(s["threads"], 42)
        self.assertEqual(s["gc"]["young"], [10, 0.25])
        self.assertEqual(s["gc"]["full"], [1, 1.0])
        self.assertEqual(s["gc"]["other"][0], 4)

    def test_little_endian(self):
        self.check(True)

    def test_big_endian(self):
        self.check(False)

    def test_parallel_heap_max_is_sum(self):
        c = self.counters_g1()
        c["sun.gc.generation.0.maxCapacity"] = 100
        c["sun.gc.generation.1.maxCapacity"] = 300
        path = os.path.join(self.dir, "2")
        with open(path, "wb") as f:
            f.write(make_hsperf(c))
        self.assertEqual(hsperf.summarize(hsperf.PerfData(path).read())["heap_max"], 400)

    def test_rejects_garbage(self):
        path = os.path.join(self.dir, "3")
        with open(path, "wb") as f:
            f.write(b"\0" * 64)
        with self.assertRaises(ValueError):
            hsperf.PerfData(path).read()

    @unittest.skipUnless(shutil.which("java") and shutil.which("javac"), "needs a JDK")
    def test_real_jvm(self):
        src = os.path.join(self.dir, "Idle.java")
        with open(src, "w") as f:
            f.write("public class Idle { public static void main(String[] a) throws Exception {"
                    " byte[][] k = new byte[64][]; for (int i = 0; ; i++) {"
                    " k[i % 64] = new byte[1 << 16]; Thread.sleep(5); } } }")
        subprocess.check_call(["javac", "-d", self.dir, src], stderr=subprocess.DEVNULL)
        p = subprocess.Popen(["java", "-Xmx64m", "-cp", self.dir, "Idle"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            path = None
            for _ in range(100):
                path = hsperf.find_file(p.pid, p.pid)
                if path:
                    break
                time.sleep(0.1)
            self.assertIsNotNone(path, "JVM did not create a hsperfdata file")
            time.sleep(0.5)
            s = hsperf.summarize(hsperf.PerfData(path).read())
            self.assertGreater(s["heap_used"], 0)
            self.assertLessEqual(s["heap_max"], 64 << 20)
            self.assertGreater(s["threads"], 0)
        finally:
            p.kill()
            p.wait()


if __name__ == "__main__":
    unittest.main()
