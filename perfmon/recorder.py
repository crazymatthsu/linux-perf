"""The sampling loop: discover targets, read counters, append CSV rows.

A recording ("run") is a directory:

    data/20261009-101500_my-test/
        meta.json        run metadata (host, interval, targets, start/end)
        host.csv         host-wide CPU / memory / network
        containers.csv   one row per container per sample (cgroup counters)
        processes.csv    one row per matched process per sample (/proc)
        jvm.csv          heap / GC per JVM per sample (hsperfdata)
        markers.csv      user annotations + process/container start/stop events
        recorder.log
"""

import csv
import json
import logging
import os
import pwd
import re
import signal
import socket
import struct
import threading
import time

from . import __version__, hsperf, procfs
from .cgroups import Cgroups
from .containers import ContainerWatcher

log = logging.getLogger("perfmon")

MB = 1024.0 * 1024.0

HOST_COLS = ["time", "epoch", "cpu_pct", "iowait_pct", "steal_pct", "load1",
             "mem_used_mb", "mem_total_mb", "mem_avail_mb", "swap_used_mb",
             "net_rx_mbps", "net_tx_mbps", "ctx_switches_ps", "collector_cpu_pct"]
CONTAINER_COLS = ["time", "epoch", "container", "image", "cpu_pct", "cpu_limit_cores",
                  "throttled_pct", "mem_used_mb", "mem_limit_mb", "mem_pct", "mem_cache_mb",
                  "net_rx_mbps", "net_tx_mbps", "disk_read_mbs", "disk_write_mbs", "pids"]
PROCESS_COLS = ["time", "epoch", "series", "target", "container", "pid", "name",
                "cpu_pct", "user_pct", "sys_pct", "rss_mb", "swap_mb", "vsz_mb",
                "threads", "fds"]
JVM_COLS = ["time", "epoch", "series", "target", "container", "pid",
            "heap_used_mb", "heap_committed_mb", "heap_max_mb", "young_used_mb",
            "old_used_mb", "metaspace_mb", "gc_young_count", "gc_young_ms",
            "gc_full_count", "gc_full_ms", "gc_other_count", "gc_other_ms",
            "gc_pause_pct", "java_threads"]
MARKER_COLS = ["time", "epoch", "kind", "text"]

FILES = {"host": HOST_COLS, "containers": CONTAINER_COLS, "processes": PROCESS_COLS,
         "jvm": JVM_COLS, "markers": MARKER_COLS}


def _fmt(v):
    if v is None:
        return ""
    if isinstance(v, float):
        return ("%.2f" % v).rstrip("0").rstrip(".")
    if isinstance(v, str):
        return v.replace("\r", " ").replace("\n", " ")
    return v


class CsvLog(object):
    def __init__(self, path, cols):
        new = not os.path.exists(path) or os.path.getsize(path) == 0
        self.f = open(path, "a", newline="")
        self.w = csv.writer(self.f, lineterminator="\n")
        self.cols = cols
        if new:
            self.w.writerow(cols)
            self.f.flush()

    def write(self, row):
        self.w.writerow([_fmt(row.get(c)) for c in self.cols])

    def flush(self):
        self.f.flush()

    def close(self):
        self.f.close()


def _delta(cur, prev):
    """Counter increase, or None when unknown or the counter was reset
    (e.g. a restarted container gets a fresh cgroup and network namespace)."""
    if cur is None or prev is None or cur < prev:
        return None
    return cur - prev


def _stamp(now):
    return {"time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
            "epoch": "%.3f" % now}


def add_marker(run_dir, text, kind="user", now=None):
    now = time.time() if now is None else now
    log_ = CsvLog(os.path.join(run_dir, "markers.csv"), MARKER_COLS)
    row = _stamp(now)
    row.update(kind=kind, text=text)
    log_.write(row)
    log_.close()


def write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2, sort_keys=True)
    os.replace(tmp, path)


def _owner(pid):
    try:
        uid = os.stat("%s/%d" % (procfs.PROC, pid)).st_uid
    except OSError:
        return "?"
    try:
        return "%s (uid %d)" % (pwd.getpwuid(uid).pw_name, uid)
    except KeyError:
        return "uid %d" % uid


class Proc(object):
    __slots__ = ("pid", "start", "series", "target", "container", "name", "java", "nspid",
                 "perf", "perf_warned", "perf_problem", "prev", "prev_gc")

    def __init__(self, pid, start, series, target, container, name, java, nspid):
        self.pid, self.start, self.series, self.target = pid, start, series, target
        self.container, self.name, self.java, self.nspid = container, name, java, nspid
        self.perf = None
        self.perf_warned = False
        self.perf_problem = None
        self.prev = None
        self.prev_gc = None


class Ctr(object):
    __slots__ = ("id", "name", "image", "dirs", "pid", "hostnet", "prev")

    def __init__(self, cid, name, image, dirs, pid, hostnet):
        self.id, self.name, self.image = cid, name, image
        self.dirs, self.pid, self.hostnet = dirs, pid, hostnet
        self.prev = None


class Recorder(object):
    def __init__(self, cfg, name="run", stop_event=None):
        self.cfg = cfg
        self.name = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-") or "run"
        self.stop = stop_event or threading.Event()
        self.cg = Cgroups()
        self.procs = {}       # pid -> Proc
        self.ctrs = {}        # container id -> Ctr
        self.watcher = None
        self.force_discover = False
        self.host_prev = None
        self.logs = {}
        self.run_dir = None
        self.samples = 0
        self.mem_total = procfs.read_meminfo().get("MemTotal", 0)
        self._first_discovery = True
        try:
            self._self_netns = procfs.netns_id("self")
            self._host_ifaces = set(procfs.read_net_dev(None)[2])
        except OSError:
            self._self_netns, self._host_ifaces = None, set()

    # ------------------------------------------------------------------ run dir
    def open_run(self):
        os.makedirs(self.cfg.output_dir, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        self.run_dir = os.path.join(self.cfg.output_dir, "%s_%s" % (stamp, self.name))
        os.makedirs(self.run_dir)
        self._handler = logging.FileHandler(os.path.join(self.run_dir, "recorder.log"))
        self._handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        log.addHandler(self._handler)
        for key, cols in FILES.items():
            self.logs[key] = CsvLog(os.path.join(self.run_dir, key + ".csv"), cols)
        self.meta = {
            "id": os.path.basename(self.run_dir),
            "name": self.name,
            "version": __version__,
            "host": socket.gethostname(),
            "kernel": os.uname().release,
            "cpus": os.cpu_count(),
            "mem_total_mb": round(self.mem_total / MB),
            "cgroup": self.cg.version,
            "interval": self.cfg.interval,
            "started": time.time(),
            "utc_offset": time.localtime().tm_gmtoff,
            "ended": None,
            "recorder_pid": os.getpid(),
            "config": self.cfg.path,
            "targets": [t.describe() for t in self.cfg.targets],
        }
        write_json(os.path.join(self.run_dir, "meta.json"), self.meta)
        link = os.path.join(self.cfg.output_dir, "latest")
        try:
            tmp = link + ".tmp"
            if os.path.lexists(tmp):
                os.remove(tmp)
            os.symlink(os.path.basename(self.run_dir), tmp)
            os.replace(tmp, link)
        except OSError:
            pass
        log.info("recording to %s (interval %ss, cgroup %s)", self.run_dir,
                 self.cfg.interval, self.cg.version)
        return self.run_dir

    def close_run(self):
        self.meta["ended"] = time.time()
        self.meta["samples"] = self.samples
        write_json(os.path.join(self.run_dir, "meta.json"), self.meta)
        for lg in self.logs.values():
            lg.close()
        log.info("stopped after %d samples: %s", self.samples, self.run_dir)
        log.removeHandler(self._handler)
        self._handler.close()

    def event(self, text):
        if self._first_discovery or not self.logs:
            return
        row = _stamp(time.time())
        row.update(kind="event", text=text)
        self.logs["markers"].write(row)

    # ---------------------------------------------------------------- discovery
    def discover(self):
        cfg = self.cfg
        if self.watcher is None:
            self.watcher = ContainerWatcher(cfg.docker_cmd, cfg.discover_interval)
            self.watcher.start()
            self.watcher.ready.wait(30)
        by_id = self.watcher.snapshot
        self._seen_gen = self.watcher.generation

        self_pid = os.getpid()
        members = {}
        candidates = []
        for pid in procfs.list_pids():
            if pid == self_pid:
                continue
            cid = None
            if by_id:
                try:
                    for x in procfs.container_ids_in(procfs.read_cgroup(pid)):
                        if x in by_id:
                            cid = x
                            break
                except OSError:
                    continue
            if cid:
                members.setdefault(cid, []).append(pid)
            candidates.append((pid, cid))

        # -- containers
        alive = {}
        gone_names = {old.name for cid, old in self.ctrs.items() if cid not in by_id}
        for cid, c in by_id.items():
            if cid not in members or not cfg.containers.search(c["name"]):
                continue
            old = self.ctrs.get(cid)
            if old and old.pid in members[cid]:
                alive[cid] = old
                continue
            pid = min(members[cid])
            dirs = self.cg.container_dirs(pid, cid)
            alive[cid] = Ctr(cid, c["name"], c["image"], dirs, pid, self._is_hostnet(pid))
            if old is None:
                log.info("container %s (%s)%s", c["name"], c["image"],
                         " [host network]" if alive[cid].hostnet else "")
                if c["name"] in gone_names:
                    self.event("container re-created: %s" % c["name"])
                else:
                    self.event("container up: %s" % c["name"])
        new_names = {c.name for c in alive.values()}
        for cid, old in self.ctrs.items():
            if cid not in alive:
                log.info("container gone: %s", old.name)
                if old.name not in new_names:
                    self.event("container down: %s" % old.name)
        self.ctrs = alive

        # -- processes
        tracked = {}
        used = set()
        for pid, cid in candidates:
            p = self.procs.get(pid)
            if p is not None:
                tracked[pid] = p
                used.add(p.series)
        for pid, cid in candidates:
            if pid in tracked:
                continue
            c = by_id.get(cid)
            cname = c["name"] if c else None
            info = None
            for t in cfg.targets:
                if not t.matches_container(cname, c["image"] if c else None):
                    continue
                if info is None:
                    try:
                        st = procfs.read_stat(pid)
                        argv = procfs.read_cmdline(pid)
                    except (OSError, ValueError, IndexError):
                        break
                    if not argv:          # kernel thread / zombie
                        break
                    info = (st, argv, os.path.basename(argv[0]), " ".join(argv))
                st, argv, exe, cmdline = info
                if not t.matches_process(exe, cmdline):
                    continue
                name = t.label or procfs.process_name(st["comm"], argv)
                series = "%s/%s" % (cname, name) if cname else name
                if series in used:
                    series = "%s#%d" % (series, pid)
                used.add(series)
                java = t.jvm == "yes" or (t.jvm == "auto" and procfs.is_java(st["comm"], argv))
                nspid = None
                if java:
                    try:
                        nspid = procfs.read_status(pid).get("NSpid")
                    except OSError:
                        pass
                tracked[pid] = Proc(pid, st["starttime"], series, t.name, cname or "",
                                    name, java, nspid)
                log.info("process %s pid=%d target=%s%s", series, pid, t.name,
                         " (jvm)" if java else "")
                self.event("process up: %s (pid %d)" % (series, pid))
                break
        for pid, p in self.procs.items():
            if pid not in tracked:
                log.info("process gone: %s pid=%d", p.series, pid)
                self.event("process down: %s (pid %d)" % (p.series, pid))
        self.procs = tracked

        for p in self.procs.values():
            if p.java and p.perf is None:
                path = hsperf.find_file(p.pid, p.nspid)
                problem = None
                if path and not os.access(path, os.R_OK):   # visible but owned by another uid
                    path, problem = None, "no-access"
                if path:
                    p.perf = hsperf.PerfData(path)
                    p.prev_gc = None
                    p.perf_problem = None
                    log.info("jvm metrics for %s from %s", p.series, path)
                else:
                    p.perf_problem = problem or hsperf.why_missing(p.pid)
                    if not p.perf_warned:
                        p.perf_warned = True
                        if p.perf_problem == "no-access":
                            log.warning("no jvm metrics for %s pid=%d: it runs as %s and perfmon runs as %s. "
                                        "Run perfmon as root or as that user (CPU/memory are still recorded)",
                                        p.series, p.pid, _owner(p.pid), _owner(os.getpid()))
                        else:
                            log.warning("no hsperfdata for %s pid=%d yet: JVM started with -XX:-UsePerfData or "
                                        "-XX:+PerfDisableSharedMem, or its uid has no name in the container's "
                                        "/etc/passwd - will keep retrying", p.series, p.pid)
        self.force_discover = False
        self._first_discovery = False

    def _is_hostnet(self, pid):
        ns = procfs.netns_id(pid)
        if ns is not None and self._self_netns is not None:
            return ns == self._self_netns
        try:  # unprivileged fallback: same interface list as the host
            return set(procfs.read_net_dev(pid)[2]) == self._host_ifaces
        except OSError:
            return False

    # ----------------------------------------------------------------- sampling
    def sample(self, write=True):
        now = time.time()
        mono = time.monotonic()
        stamp = _stamp(now)
        if self.cfg.host_metrics:
            self._sample_host(stamp, mono, write)
        self._sample_containers(stamp, mono, write)
        self._sample_procs(stamp, mono, write)
        if write:
            self.samples += 1
            for lg in self.logs.values():
                lg.flush()

    def _sample_host(self, stamp, mono, write):
        try:
            cpu = procfs.read_host_cpu()
            mem = procfs.read_meminfo()
            load = procfs.read_loadavg()
            rx, tx, _ = procfs.read_net_dev(None, physical_only=True)
        except OSError as e:
            log.warning("host metrics: %s", e)
            return
        t = os.times()
        own = t[0] + t[1] + t[2] + t[3]
        cur = (mono, cpu, rx, tx, own)
        prev, self.host_prev = self.host_prev, cur
        if not write:
            return
        row = dict(stamp)
        row.update(
            load1=load,
            mem_total_mb=mem.get("MemTotal", 0) / MB,
            mem_avail_mb=mem.get("MemAvailable", 0) / MB,
            mem_used_mb=(mem.get("MemTotal", 0) - mem.get("MemAvailable", 0)) / MB,
            swap_used_mb=(mem.get("SwapTotal", 0) - mem.get("SwapFree", 0)) / MB,
        )
        if prev:
            dt = mono - prev[0]
            p = prev[1]
            tot = float(cpu["total"] - p["total"]) or 1.0
            idle = cpu["idle"] - p["idle"]
            iow = cpu["iowait"] - p["iowait"]
            row.update(
                cpu_pct=100.0 * (tot - idle - iow) / tot,
                iowait_pct=100.0 * iow / tot,
                steal_pct=100.0 * (cpu["steal"] - p["steal"]) / tot,
                net_rx_mbps=(rx - prev[2]) * 8 / 1e6 / dt,
                net_tx_mbps=(tx - prev[3]) * 8 / 1e6 / dt,
                ctx_switches_ps=float(cpu["ctxt"] - p["ctxt"]) / dt,
                collector_cpu_pct=100.0 * (own - prev[4]) / dt,
            )
        self.logs["host"].write(row)

    def _sample_containers(self, stamp, mono, write):
        for c in list(self.ctrs.values()):
            v = self.cg.read(c.dirs)
            if not v:                 # cgroup not resolvable on this host
                continue
            if v["cpu_ns"] is None and v["mem_usage"] is None:
                if c.prev is not None:  # was readable before: container stopped
                    self.force_discover = True
                    self.watcher.refresh_soon()
                continue
            net = None
            if not c.hostnet:
                try:
                    net = procfs.read_net_dev(c.pid)[:2]
                except PermissionError:   # hardened /proc: just no network numbers
                    pass
                except OSError:           # member process gone
                    self.force_discover = True
            cur = (mono, v, net)
            prev, c.prev = c.prev, cur
            if not write:
                continue
            used = max(0, (v["mem_usage"] or 0) - (v["mem_inactive_file"] or 0))
            limit = v["mem_limit"]
            row = dict(stamp)
            row.update(
                container=c.name, image=c.image,
                cpu_limit_cores=v["cpu_limit"],
                mem_used_mb=used / MB,
                mem_limit_mb=limit / MB if limit else None,
                mem_pct=100.0 * used / (limit or self.mem_total or 1),
                mem_cache_mb=v["mem_cache"] / MB if v["mem_cache"] is not None else None,
                pids=v["pids"],
            )
            if prev:
                dt = mono - prev[0]
                pv = prev[1]
                d = _delta(v["cpu_ns"], pv["cpu_ns"])
                if d is not None:
                    row["cpu_pct"] = 100.0 * d / 1e9 / dt
                dp = _delta(v["periods"], pv["periods"])
                dthr = _delta(v["throttled"], pv["throttled"])
                if dp is not None and dthr is not None:
                    row["throttled_pct"] = 100.0 * dthr / dp if dp else 0.0
                if net and prev[2]:
                    rx, tx = _delta(net[0], prev[2][0]), _delta(net[1], prev[2][1])
                    if rx is not None and tx is not None:
                        row["net_rx_mbps"] = rx * 8 / 1e6 / dt
                        row["net_tx_mbps"] = tx * 8 / 1e6 / dt
                rd, wr = _delta(v["io_read"], pv["io_read"]), _delta(v["io_write"], pv["io_write"])
                if rd is not None and wr is not None:
                    row["disk_read_mbs"] = rd / MB / dt
                    row["disk_write_mbs"] = wr / MB / dt
            self.logs["containers"].write(row)

    def _sample_procs(self, stamp, mono, write):
        for pid, p in list(self.procs.items()):
            try:
                st = procfs.read_stat(pid)
                status = procfs.read_status(pid)
            except (OSError, ValueError, IndexError):
                st = None
            if st is None or st["starttime"] != p.start or st["state"] == "Z":
                del self.procs[pid]
                log.info("process gone: %s pid=%d", p.series, pid)
                self.event("process down: %s (pid %d)" % (p.series, pid))
                self.force_discover = True
                continue
            cur = (mono, st["utime"], st["stime"])
            prev, p.prev = p.prev, cur
            jvm = self._read_jvm(p, mono)
            if not write:
                continue
            row = dict(stamp)
            row.update(series=p.series, target=p.target, container=p.container, pid=pid,
                       name=p.name, rss_mb=st["rss"] / MB, vsz_mb=st["vsize"] / MB,
                       swap_mb=status.get("VmSwap", 0) / MB, threads=st["threads"],
                       fds=procfs.count_fds(pid))
            if prev:
                dt = mono - prev[0]
                u = 100.0 * (st["utime"] - prev[1]) / procfs.CLK_TCK / dt
                s = 100.0 * (st["stime"] - prev[2]) / procfs.CLK_TCK / dt
                row.update(cpu_pct=u + s, user_pct=u, sys_pct=s)
            self.logs["processes"].write(row)
            if jvm:
                jrow = dict(stamp)
                jrow.update(series=p.series, target=p.target, container=p.container, pid=pid)
                jrow.update(jvm)
                self.logs["jvm"].write(jrow)

    def _read_jvm(self, p, mono):
        if p.perf is None:
            return None
        try:
            s = hsperf.summarize(p.perf.read())
        except (OSError, ValueError, struct.error) as e:
            log.warning("jvm metrics for %s stopped: %s", p.series, e)
            p.perf = None
            p.perf_warned = False
            return None
        cur = (mono, s["gc"])
        prev, p.prev_gc = p.prev_gc, cur
        row = {
            "heap_used_mb": s["heap_used"] / MB,
            "heap_committed_mb": s["heap_committed"] / MB,
            "heap_max_mb": s["heap_max"] / MB if s["heap_max"] else None,
            "young_used_mb": s["young_used"] / MB,
            "old_used_mb": s["old_used"] / MB,
            "metaspace_mb": s["metaspace"] / MB if s["metaspace"] is not None else None,
            "java_threads": s["threads"],
        }
        if prev:
            dt = mono - prev[0]
            pause = 0.0
            for kind in ("young", "full", "other"):
                n = _delta(s["gc"][kind][0], prev[1][kind][0]) or 0
                secs = _delta(s["gc"][kind][1], prev[1][kind][1]) or 0.0
                pause += secs
                row["gc_%s_count" % kind] = n
                row["gc_%s_ms" % kind] = secs * 1000.0
            row["gc_pause_pct"] = 100.0 * pause / dt
        return row

    # --------------------------------------------------------------------- loop
    def run(self, duration=None):
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            try:
                signal.signal(sig, lambda *_: self.stop.set())
            except ValueError:  # not in main thread (tests)
                pass
        self.open_run()
        try:
            self.discover()
            next_disc = time.monotonic() + self.cfg.discover_interval
            self.sample(write=False)          # prime counters so row 1 has rates
            t0 = time.monotonic()
            k = 0
            while True:
                k += 1
                delay = t0 + k * self.cfg.interval - time.monotonic()
                if delay < 0:                 # fell behind: skip missed ticks
                    k = int((time.monotonic() - t0) / self.cfg.interval) + 1
                    delay = t0 + k * self.cfg.interval - time.monotonic()
                if self.stop.wait(delay):
                    break
                now = time.monotonic()
                if self.force_discover or now >= next_disc or self.watcher.generation != self._seen_gen:
                    self.discover()
                    next_disc = now + self.cfg.discover_interval
                self.sample()
                if self.samples == 1:
                    log.info("first sample written: %d containers, %d processes (%d jvm)",
                             len(self.ctrs), len(self.procs),
                             sum(1 for p in self.procs.values() if p.perf))
                if duration and time.monotonic() - t0 >= duration:
                    break
        finally:
            if self.watcher:
                self.watcher.stop()
            self.close_run()
        return self.run_dir
