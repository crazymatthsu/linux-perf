"""Container-level counters read straight from the cgroup filesystem.

Supports cgroup v1 (incl. systemd "hybrid" hosts) and cgroup v2, with both
the cgroupfs and systemd cgroup drivers. Values are cumulative; the recorder
turns them into rates.
"""

import os

from . import procfs

_V1_CONTROLLERS = ("cpu", "cpuacct", "memory", "blkio", "pids")


def _read_int(path):
    try:
        with open(path) as f:
            return int(f.read().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def _read_kv(path):
    out = {}
    try:
        with open(path) as f:
            for line in f:
                parts = line.split()
                if len(parts) == 2:
                    try:
                        out[parts[0]] = int(parts[1])
                    except ValueError:
                        pass
    except OSError:
        pass
    return out


def _cut_after(path, container_id):
    """'/machine.slice/libpod-<id>.scope/container' -> '/machine.slice/libpod-<id>.scope'.

    The container's own cgroup aggregates any sub-cgroups (crun's
    "container" child, systemd's init.scope inside the container, ...).
    """
    segs = path.split("/")
    for i, s in enumerate(segs):
        if procfs.container_segment_id(s) == container_id:
            return "/".join(segs[:i + 1])
    return path


class Cgroups(object):
    def __init__(self):
        self.v1 = {}      # controller -> mount point
        self.v2 = None    # unified hierarchy mount point
        try:
            with open(procfs.PROC + "/self/mounts") as f:
                mounts = f.read().splitlines()
        except OSError:
            mounts = []
        for line in mounts:
            parts = line.split()
            if len(parts) < 4:
                continue
            mnt, fstype, opts = parts[1], parts[2], parts[3].split(",")
            if fstype == "cgroup2" and self.v2 is None:
                self.v2 = mnt
            elif fstype == "cgroup":
                for c in _V1_CONTROLLERS:
                    if c in opts and c not in self.v1:
                        self.v1[c] = mnt
        if "memory" in self.v1 or "cpuacct" in self.v1:
            self.version = "v1"
        elif self.v2 and os.path.exists(os.path.join(self.v2, "cgroup.controllers")):
            self.version = "v2"
        else:
            self.version = None

    def container_dirs(self, pid, container_id):
        """Map of controller -> cgroup directory for the container owning pid."""
        dirs = {}
        try:
            text = procfs.read_cgroup(pid)
        except OSError:
            return dirs
        for line in text.splitlines():
            parts = line.split(":", 2)
            if len(parts) != 3:
                continue
            hid, ctrls, path = parts
            path = _cut_after(path, container_id)
            if self.version == "v2" and hid == "0" and ctrls == "":
                dirs["unified"] = self.v2 + path
            elif self.version == "v1":
                for c in ctrls.split(","):
                    if c in self.v1:
                        dirs[c] = self.v1[c] + path
        return dirs

    def read(self, dirs):
        if "unified" in dirs:
            return self._read_v2(dirs["unified"])
        if dirs:
            return self._read_v1(dirs)
        return None

    @staticmethod
    def _read_v2(d):
        cpu = _read_kv(os.path.join(d, "cpu.stat"))
        mem = _read_kv(os.path.join(d, "memory.stat"))
        out = {
            "cpu_ns": cpu["usage_usec"] * 1000 if "usage_usec" in cpu else None,
            "periods": cpu.get("nr_periods"),
            "throttled": cpu.get("nr_throttled"),
            "cpu_limit": None,
            "mem_usage": _read_int(os.path.join(d, "memory.current")),
            "mem_limit": _read_int(os.path.join(d, "memory.max")),  # "max" -> None
            "mem_inactive_file": mem.get("inactive_file", 0),
            "mem_cache": mem.get("file"),
            "io_read": 0,
            "io_write": 0,
            "pids": _read_int(os.path.join(d, "pids.current")),
        }
        try:
            with open(os.path.join(d, "cpu.max")) as f:
                quota, period = f.read().split()[:2]
            if quota != "max":
                out["cpu_limit"] = float(quota) / float(period)
        except (OSError, ValueError):
            pass
        try:
            with open(os.path.join(d, "io.stat")) as f:
                for line in f:
                    for kv in line.split()[1:]:
                        k, _, v = kv.partition("=")
                        if k == "rbytes":
                            out["io_read"] += int(v)
                        elif k == "wbytes":
                            out["io_write"] += int(v)
        except (OSError, ValueError):
            out["io_read"] = out["io_write"] = None
        return out

    @staticmethod
    def _read_v1(dirs):
        cpu_d = dirs.get("cpu", "")
        acct_d = dirs.get("cpuacct", cpu_d)
        mem_d = dirs.get("memory", "")
        cpu = _read_kv(os.path.join(cpu_d, "cpu.stat"))
        mem = _read_kv(os.path.join(mem_d, "memory.stat"))
        limit = _read_int(os.path.join(mem_d, "memory.limit_in_bytes"))
        if limit is not None and limit >= 1 << 60:
            limit = None
        out = {
            "cpu_ns": _read_int(os.path.join(acct_d, "cpuacct.usage")),
            "periods": cpu.get("nr_periods"),
            "throttled": cpu.get("nr_throttled"),
            "cpu_limit": None,
            "mem_usage": _read_int(os.path.join(mem_d, "memory.usage_in_bytes")),
            "mem_limit": limit,
            "mem_inactive_file": mem.get("total_inactive_file", mem.get("inactive_file", 0)),
            "mem_cache": mem.get("total_cache", mem.get("cache")),
            "io_read": None,
            "io_write": None,
            "pids": _read_int(os.path.join(dirs.get("pids", ""), "pids.current")),
        }
        quota = _read_int(os.path.join(cpu_d, "cpu.cfs_quota_us"))
        period = _read_int(os.path.join(cpu_d, "cpu.cfs_period_us"))
        if quota and quota > 0 and period:
            out["cpu_limit"] = float(quota) / period
        blk = dirs.get("blkio")
        if blk:
            for name in ("blkio.throttle.io_service_bytes_recursive",
                         "blkio.throttle.io_service_bytes",
                         "blkio.io_service_bytes_recursive"):
                try:
                    with open(os.path.join(blk, name)) as f:
                        lines = f.read().splitlines()
                except OSError:
                    continue
                rd = wr = 0
                for line in lines:
                    p = line.split()
                    if len(p) == 3 and p[1] == "Read":
                        rd += int(p[2])
                    elif len(p) == 3 and p[1] == "Write":
                        wr += int(p[2])
                out["io_read"], out["io_write"] = rd, wr
                break
        return out
