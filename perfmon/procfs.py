"""Readers for /proc: per-process and host-wide counters.

Every function returns plain numbers (cumulative counters where the kernel
exposes counters); rate calculation happens in the recorder.
"""

import os
import re

PROC = os.environ.get("PERFMON_PROC", "/proc")
CLK_TCK = os.sysconf("SC_CLK_TCK")
PAGE_SIZE = os.sysconf("SC_PAGE_SIZE")

# A cgroup path segment that *is* a container's cgroup:
#   <id>                       docker/podman cgroupfs driver (/docker/<id>)
#   docker-<id>.scope          docker, systemd driver
#   libpod-<id>.scope          podman, systemd cgroup manager (RHEL default)
#   cri-containerd-<id>.scope, crio-<id>.scope
# Podman/CRI-O run their monitor in "libpod-conmon-<id>.scope" /
# "crio-conmon-<id>.scope": same id, but not the container, so excluded.
_CONTAINER_SEG_RE = re.compile(r"^(?:[A-Za-z0-9_]+(?:-[A-Za-z0-9_]+)*-)?([0-9a-f]{64})(?:\.scope)?$")

# Interfaces that only carry traffic already counted on a physical NIC
# (docker bridge, veth pairs, CNI overlays) or that never leave the box.
_VIRTUAL_IFACE_RE = re.compile(
    r"^(lo|docker\d*|veth|br-|virbr|cni|flannel|cali|kube|tunl|vxlan|weave|podman)"
)

# Options that take a separate value argument on the java command line.
_JAVA_OPTS_WITH_VALUE = {
    "-cp", "-classpath", "--class-path", "-p", "--module-path",
    "--upgrade-module-path", "--add-modules", "--limit-modules",
    "--add-exports", "--add-opens", "--add-reads", "--patch-module",
    "--enable-native-access", "--source",
}


def _read(path):
    with open(path, "rb") as f:
        return f.read().decode("utf-8", "replace")


def list_pids():
    return [int(d) for d in os.listdir(PROC) if d.isdigit()]


def read_stat(pid):
    """Parse /proc/<pid>/stat. Raises OSError if the process is gone."""
    data = _read("%s/%d/stat" % (PROC, pid))
    # comm may contain spaces or ')' so split on the *last* ')'.
    rp = data.rindex(")")
    comm = data[data.index("(") + 1:rp]
    f = data[rp + 2:].split()
    # f[0] is field 3 (state); field N of proc(5) is f[N - 3].
    return {
        "comm": comm,
        "state": f[0],
        "ppid": int(f[1]),
        "majflt": int(f[9]),
        "utime": int(f[11]),
        "stime": int(f[12]),
        "threads": int(f[17]),
        "starttime": int(f[19]),
        "vsize": int(f[20]),
        "rss": int(f[21]) * PAGE_SIZE,
    }


_STATUS_KEYS = ("VmSwap", "RssAnon", "RssFile", "RssShmem", "NSpid")


def read_status(pid):
    """Selected fields from /proc/<pid>/status (sizes in bytes)."""
    out = {}
    for line in _read("%s/%d/status" % (PROC, pid)).splitlines():
        key, _, val = line.partition(":")
        if key not in _STATUS_KEYS:
            continue
        val = val.split()
        if key == "NSpid":
            out[key] = int(val[-1])  # innermost pid namespace = pid inside the container
        elif val:
            out[key] = int(val[0]) * 1024
    return out


def read_cmdline(pid):
    with open("%s/%d/cmdline" % (PROC, pid), "rb") as f:
        raw = f.read()
    return [a.decode("utf-8", "replace") for a in raw.split(b"\0") if a]


def count_fds(pid):
    """Number of open file descriptors, or None without permission."""
    try:
        return len(os.listdir("%s/%d/fd" % (PROC, pid)))
    except OSError:
        return None


def read_cgroup(pid):
    return _read("%s/%d/cgroup" % (PROC, pid))


def container_segment_id(segment):
    """Container id if this cgroup path segment is a container's own cgroup."""
    m = _CONTAINER_SEG_RE.match(segment)
    if m and "conmon" not in segment:
        return m.group(1)
    return None


def container_ids_in(text):
    """Container ids found in /proc/<pid>/cgroup content."""
    ids = []
    for line in text.splitlines():
        for seg in line.split(":", 2)[-1].split("/"):
            cid = container_segment_id(seg)
            if cid and cid not in ids:
                ids.append(cid)
    return ids


def java_main(argv):
    """Main class (short name), jar or module of a java command line."""
    i = 1
    while i < len(argv):
        a = argv[i]
        if a in ("-jar",) and i + 1 < len(argv):
            return os.path.basename(argv[i + 1])
        if a in ("-m", "--module") and i + 1 < len(argv):
            return argv[i + 1].split("/")[-1]
        if a in _JAVA_OPTS_WITH_VALUE:
            i += 2
            continue
        if a.startswith("-") or a.startswith("@"):
            i += 1
            continue
        return a.split(".")[-1] if not a.endswith(".java") else os.path.basename(a)
    return None


def is_java(comm, argv):
    exe = os.path.basename(argv[0]) if argv else comm
    return exe == "java" or comm == "java"


def process_name(comm, argv):
    """Readable label: java main class/jar for JVMs, else the executable."""
    if is_java(comm, argv):
        main = java_main(argv)
        return "java:%s" % main if main else "java"
    exe = os.path.basename(argv[0]) if argv else comm
    # Interpreters: show the script, e.g. "python:load_driver.py"
    if re.match(r"^(python[\d.]*|perl|ruby|node|bash|sh)$", exe) and len(argv) > 1:
        for a in argv[1:]:
            if not a.startswith("-"):
                return "%s:%s" % (exe, os.path.basename(a))
    return (exe or comm)[:60]


# --------------------------------------------------------------------------
# Host-wide
# --------------------------------------------------------------------------

def read_host_cpu():
    """Cumulative jiffies: (total, idle, iowait, steal) and context switches."""
    total = idle = iowait = steal = 0
    ctxt = 0
    for line in _read(PROC + "/stat").splitlines():
        if line.startswith("cpu "):
            v = [int(x) for x in line.split()[1:]]
            v += [0] * (8 - len(v))
            # user nice system idle iowait irq softirq steal (guest is in user)
            total = sum(v[:8])
            idle, iowait, steal = v[3], v[4], v[7]
        elif line.startswith("ctxt "):
            ctxt = int(line.split()[1])
    return {"total": total, "idle": idle, "iowait": iowait, "steal": steal, "ctxt": ctxt}


def read_meminfo():
    out = {}
    for line in _read(PROC + "/meminfo").splitlines():
        key, _, val = line.partition(":")
        val = val.split()
        if val:
            out[key] = int(val[0]) * 1024
    if "MemAvailable" not in out:  # kernels < 3.14
        out["MemAvailable"] = out.get("MemFree", 0) + out.get("Cached", 0) + out.get("Buffers", 0)
    return out


def read_loadavg():
    return float(_read(PROC + "/loadavg").split()[0])


def read_net_dev(pid=None, physical_only=False):
    """Total (rx_bytes, tx_bytes, iface_names) for a network namespace.

    pid=None reads the collector's own (normally the host) namespace;
    otherwise the namespace of that process.
    """
    path = "%s/%s/net/dev" % (PROC, pid if pid is not None else "self")
    rx = tx = 0
    names = []
    for line in _read(path).splitlines()[2:]:
        name, _, rest = line.partition(":")
        name = name.strip()
        names.append(name)
        if name == "lo" or (physical_only and _VIRTUAL_IFACE_RE.match(name)):
            continue
        v = rest.split()
        rx += int(v[0])
        tx += int(v[8])
    return rx, tx, names


def netns_id(pid):
    """Inode of the process's network namespace, or None without permission."""
    try:
        return os.readlink("%s/%s/ns/net" % (PROC, pid))
    except OSError:
        return None
