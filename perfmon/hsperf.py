"""Zero-overhead JVM heap / GC metrics from the HotSpot perf-data file.

Every HotSpot JVM (unless started with -XX:-UsePerfData or
-XX:+PerfDisableSharedMem) maintains /tmp/hsperfdata_<user>/<pid>, a
memory-mapped file holding the same counters `jstat` prints. We read it from
the host through /proc/<host-pid>/root, so the container needs no JDK tools,
no JMX port and no agent.
"""

import glob
import re
import struct

from . import procfs

_MAGIC = 0xCAFEC0C0
_WANTED = re.compile(
    r"^(sun\.gc\.generation\.\d+\.(capacity|maxCapacity|space\.\d+\.used)"
    r"|sun\.gc\.collector\.\d+\.(invocations|time)"
    r"|sun\.gc\.metaspace\.used|sun\.os\.hrt\.frequency|java\.threads\.live)$"
)


def find_file(pid, nspid):
    """Locate the perf-data file of a (possibly containerized) JVM."""
    root = "%s/%d/root/tmp" % (procfs.PROC, pid)
    for cand in (nspid, pid):
        if cand is None:
            continue
        hits = glob.glob("%s/hsperfdata_*/%d" % (root, cand))
        if hits:
            return hits[0]
    return None


class PerfData(object):
    """Parses the file once, then re-reads only the counters it needs."""

    def __init__(self, path):
        self.path = path
        self._layout = None   # [(name, offset)] of long counters we care about
        self._key = None      # (num_entries, mod_time) the layout belongs to
        self._endian = "<"

    def _parse_layout(self, d):
        e = self._endian
        _, _, mod_time, entry_off, n = struct.unpack_from(e + "iiqii", d, 8)
        layout = []
        off = entry_off
        for _ in range(n):
            if off + 20 > len(d):
                break
            elen, name_off, vlen = struct.unpack_from(e + "iii", d, off)
            if elen <= 0:
                break
            dtype = d[off + 12]
            data_off = struct.unpack_from(e + "i", d, off + 16)[0]
            end = d.find(b"\0", off + name_off)
            name = d[off + name_off:end].decode("ascii", "replace")
            if dtype == ord("J") and vlen == 0 and _WANTED.match(name):
                layout.append((name, off + data_off))
            off += elen
        self._layout = layout
        self._key = (n, mod_time)

    def read(self):
        """Dict of counter name -> int. Raises OSError/ValueError on failure."""
        with open(self.path, "rb") as f:
            d = f.read()
        if len(d) < 32 or struct.unpack_from(">I", d, 0)[0] != _MAGIC:
            raise ValueError("not a hsperfdata file")
        self._endian = "<" if d[4] == 1 else ">"
        key = (struct.unpack_from(self._endian + "i", d, 28)[0],
               struct.unpack_from(self._endian + "q", d, 16)[0])
        if key != self._key:
            self._parse_layout(d)
        e = self._endian + "q"
        return {name: struct.unpack_from(e, d, off)[0] for name, off in self._layout}


def summarize(c):
    """Turn raw counters into heap / GC figures (bytes, counts, seconds)."""
    gens = {}
    for name, val in c.items():
        m = re.match(r"sun\.gc\.generation\.(\d+)\.(capacity|maxCapacity|space\.\d+\.used)$", name)
        if m:
            g = gens.setdefault(int(m.group(1)), {"used": 0, "capacity": 0, "max": 0})
            if m.group(2) == "capacity":
                g["capacity"] = val
            elif m.group(2) == "maxCapacity":
                g["max"] = val
            else:
                g["used"] += val
    young = gens.get(0, {}).get("used", 0)
    old = sum(g["used"] for k, g in gens.items() if k >= 1)
    maxes = [g["max"] for g in gens.values() if g["max"]]
    # G1 / generational ZGC report the whole heap as each generation's max;
    # Parallel / Serial split it between generations.
    if maxes and len(set(maxes)) == 1:
        heap_max = maxes[0]
    else:
        heap_max = sum(maxes)

    freq = float(c.get("sun.os.hrt.frequency") or 1e9)
    gc = {"young": [0, 0.0], "full": [0, 0.0], "other": [0, 0.0]}
    for name, val in c.items():
        m = re.match(r"sun\.gc\.collector\.(\d+)\.(invocations|time)$", name)
        if m:
            idx = int(m.group(1))
            kind = "young" if idx == 0 else "full" if idx == 1 else "other"
            if m.group(2) == "invocations":
                gc[kind][0] += val
            else:
                gc[kind][1] += val / freq
    return {
        "heap_used": young + old,
        "heap_committed": sum(g["capacity"] for g in gens.values()),
        "heap_max": heap_max or None,
        "young_used": young,
        "old_used": old,
        "metaspace": c.get("sun.gc.metaspace.used"),
        "threads": c.get("java.threads.live"),
        "gc": gc,
    }
