"""Recorder -> CSV -> web server / report, end to end on host processes.

Run from the repository root:  python3 -m unittest discover -s tests -v
"""

import base64
import csv
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from perfmon import report, server  # noqa: E402
from perfmon.config import Config  # noqa: E402
from perfmon.recorder import Recorder, add_marker  # noqa: E402

BURNER = "import time\nwhile True:\n    sum(range(10000))\n"


def write_config(dirname, extra=""):
    path = os.path.join(dirname, "perfmon.conf")
    with open(path, "w") as f:
        f.write("[perfmon]\ninterval = 0.5\noutput_dir = data\ndocker_cmd = false\n" + extra)
    return path


class ConfigTest(unittest.TestCase):
    def test_example_config_parses(self):
        cfg = Config(os.path.join(ROOT, "perfmon.conf.example"))
        names = [t.name for t in cfg.targets]
        self.assertEqual(names[:3], ["deephaven", "amps", "java-apps"])
        dh, amps, apps = cfg.targets[:3]
        self.assertTrue(dh.matches_container("deephaven", "ghcr.io/deephaven/server"))
        self.assertTrue(dh.matches_process("java", "java -cp x io.deephaven.server.jetty.JettyMain"))
        self.assertFalse(dh.matches_process("sh", "sh -c java ..."))
        self.assertTrue(amps.matches_process("ampServer", "/AMPS/bin/ampServer config.xml"))
        self.assertTrue(apps.matches_container("app-orders", "x"))
        self.assertFalse(apps.matches_container("deephaven", "x"))
        self.assertFalse(apps.matches_container(None, None))   # needs a container

    def test_default_target_skips_wrappers(self):
        cfg = Config(None)
        self.assertTrue(cfg.default_targets)
        t = cfg.targets[0]
        self.assertFalse(t.matches_process("sh", "/bin/sh -c exec java -jar app.jar"))
        self.assertFalse(t.matches_process("tini", "/usr/bin/tini -- /start.sh"))
        self.assertTrue(t.matches_process("java", "java -jar app.jar"))


class PipelineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp()
        cls.tag = "perfmon-test-%d" % os.getpid()
        cls.burner = subprocess.Popen([sys.executable, "-c", BURNER, cls.tag])
        conf = write_config(cls.dir, "[target burner]\nprocess = %s\nlabel = burner\n" % cls.tag)
        cls.cfg = Config(conf)
        rec = Recorder(cls.cfg, name="unit test")
        cls.run_dir = rec.run(duration=2.2)
        add_marker(cls.run_dir, 'phase 2, "quoted"')

    @classmethod
    def tearDownClass(cls):
        cls.burner.kill()
        cls.burner.wait()
        shutil.rmtree(cls.dir)

    def rows(self, name):
        with open(os.path.join(self.run_dir, name + ".csv"), newline="") as f:
            return list(csv.DictReader(f))

    def test_files_and_meta(self):
        with open(os.path.join(self.run_dir, "meta.json")) as f:
            meta = json.load(f)
        self.assertEqual(meta["name"], "unit-test")
        self.assertIsNotNone(meta["ended"])
        self.assertGreaterEqual(meta["samples"], 3)
        self.assertTrue(os.path.basename(self.run_dir).endswith("_unit-test"))
        self.assertEqual(os.path.realpath(os.path.join(self.cfg.output_dir, "latest")),
                         os.path.realpath(self.run_dir))

    def test_process_rows(self):
        rows = [r for r in self.rows("processes") if r["series"] == "burner"]
        self.assertGreaterEqual(len(rows), 3)
        self.assertEqual(rows[0]["pid"], str(self.burner.pid))
        cpu = [float(r["cpu_pct"]) for r in rows if r["cpu_pct"]]
        self.assertEqual(len(cpu), len(rows), "every row should carry a CPU rate")
        self.assertGreater(max(cpu), 50.0)          # a busy loop pins one core
        self.assertGreater(float(rows[-1]["rss_mb"]), 1.0)

    def test_host_rows(self):
        rows = self.rows("host")
        self.assertGreaterEqual(len(rows), 3)
        self.assertTrue(all(r["cpu_pct"] for r in rows))
        self.assertGreater(float(rows[-1]["mem_total_mb"]), 0)

    def test_marker_csv_quoting(self):
        rows = self.rows("markers")
        self.assertEqual(rows[-1]["text"], 'phase 2, "quoted"')

    def test_summary_and_resample(self):
        s = report.summarize(self.run_dir)
        burner = [p for p in s["processes"] if p["series"] == "burner"][0]
        self.assertGreater(burner["cpu"]["max"], 50)
        with open(os.path.join(self.run_dir, "processes.csv")) as f:
            text = f.read()
        raw = [r for r in csv.DictReader(io.StringIO(text)) if r["series"] == "burner"]
        out = [r for r in csv.DictReader(io.StringIO(report.resample_csv(text, "series", 60)))
               if r["series"] == "burner"]
        # Buckets are aligned to the clock, so a short run may straddle a minute.
        buckets = sorted({int(float(r["epoch"]) // 60) * 60 for r in raw})
        self.assertEqual([int(float(r["epoch"])) for r in out], buckets)
        first = [float(r["cpu_pct"]) for r in raw if int(float(r["epoch"]) // 60) * 60 == buckets[0]]
        self.assertAlmostEqual(float(out[0]["cpu_pct"]), sum(first) / len(first), delta=0.01)

    def test_report_is_self_contained(self):
        html = report.build_report_html(self.run_dir)
        self.assertIn("window.PERFMON_EMBED", html)
        self.assertNotIn('src="static/', html)
        self.assertNotIn('href="static/', html)
        self.assertNotIn("<script src=", html)


class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp()
        cls.cfg = Config(write_config(cls.dir))
        run = os.path.join(cls.cfg.output_dir, "20260101-000000_demo")
        os.makedirs(run)
        with open(os.path.join(run, "meta.json"), "w") as f:
            json.dump({"id": "20260101-000000_demo", "name": "demo", "started": 1.0, "ended": 2.0,
                       "interval": 1}, f)
        with open(os.path.join(run, "host.csv"), "w") as f:
            f.write("time,epoch,cpu_pct\nx,1.0,10\nx,2.0,20\nx,3.0,3")   # last line incomplete
        server.Handler.data_dir = cls.cfg.output_dir
        server.Handler.log_message = lambda *a: None
        server.Handler.auth = None
        cls.httpd = server._Server(("127.0.0.1", 0), server.Handler)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        shutil.rmtree(cls.dir)

    def get(self, path, headers=None):
        req = urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, path), headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.read(), r.headers
        except urllib.error.HTTPError as e:
            return e.code, e.read(), e.headers

    def test_index_and_static(self):
        code, body, _ = self.get("/")
        self.assertEqual(code, 200)
        self.assertIn(b"static/app.js", body)
        self.assertEqual(self.get("/static/chart.js")[0], 200)
        self.assertEqual(self.get("/static/../server.py")[0], 404)
        self.assertEqual(self.get("/static/index.html")[0], 404)

    def test_runs_and_tail(self):
        code, body, _ = self.get("/api/runs")
        runs = json.loads(body)
        self.assertEqual([r["id"] for r in runs], ["20260101-000000_demo"])
        self.assertFalse(runs[0]["live"])
        code, body, h = self.get("/api/runs/20260101-000000_demo/host.csv?offset=0")
        self.assertEqual(body, b"time,epoch,cpu_pct\nx,1.0,10\nx,2.0,20\n")   # complete lines only
        nxt = int(h["X-Next-Offset"])
        code, body, h = self.get("/api/runs/20260101-000000_demo/host.csv?offset=%d" % nxt)
        self.assertEqual(body, b"")
        self.assertEqual(int(h["X-Next-Offset"]), nxt)

    def test_rejects_traversal_and_unknown_files(self):
        for p in ("/api/runs/..%2f..%2fetc/passwd.csv", "/api/runs/../meta.json",
                  "/api/runs/20260101-000000_demo/recorder.csv",
                  "/api/runs/.hidden/host.csv", "/api/runs/nope/host.csv"):
            self.assertEqual(self.get(p)[0], 404, p)

    def test_basic_auth(self):
        server.Handler.auth = "load:test"
        try:
            self.assertEqual(self.get("/api/runs")[0], 401)
            bad = base64.b64encode(b"load:nope").decode()
            self.assertEqual(self.get("/api/runs", {"Authorization": "Basic " + bad})[0], 401)
            good = base64.b64encode(b"load:test").decode()
            self.assertEqual(self.get("/api/runs", {"Authorization": "Basic " + good})[0], 200)
        finally:
            server.Handler.auth = None

    def test_report_download(self):
        code, body, h = self.get("/api/runs/20260101-000000_demo/report.html")
        self.assertEqual(code, 200)
        self.assertIn("attachment", h["Content-Disposition"])
        self.assertIn(b"PERFMON_EMBED", body)


if __name__ == "__main__":
    unittest.main()
