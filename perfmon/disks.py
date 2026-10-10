"""Disk space for configured folders (default /logs and /apps).

Two views per folder:
  * the filesystem it lives on, like `df`: size / used / free / inodes. One
    statvfs() call, so it is sampled every interval.
  * the folder's own size, like `du -sx`: allocated bytes and file count.
    Walking a large tree costs I/O, so it runs in a background thread at low
    priority every `disk_scan_interval` seconds and never stalls sampling.
"""

import logging
import os
import stat
import threading
import time

from . import procfs

log = logging.getLogger("perfmon")


def parse_paths(text):
    """'/logs, /apps' or one path per line -> ['/logs', '/apps']."""
    out = []
    for part in (text or "").replace("\n", ",").split(","):
        p = part.strip()
        if p:
            p = os.path.abspath(os.path.expanduser(p))
            if p not in out:
                out.append(p)
    return out


def mount_of(path):
    """Mount point of the filesystem holding `path` (longest matching mount)."""
    real = os.path.realpath(path)
    best = "/"
    try:
        with open(procfs.PROC + "/self/mounts") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 2:
                    continue
                mnt = parts[1].replace("\\040", " ")
                if (real == mnt or real.startswith(mnt.rstrip("/") + "/")) and len(mnt) > len(best):
                    best = mnt
    except OSError:
        pass
    return best


def fs_usage(path):
    """df-style numbers in bytes; used_pct matches df's Use% (reserved blocks excluded)."""
    st = os.statvfs(path)
    size = st.f_blocks * st.f_frsize
    free_all = st.f_bfree * st.f_frsize
    avail = st.f_bavail * st.f_frsize
    used = size - free_all
    out = {
        "size": size,
        "used": used,
        "avail": avail,
        "used_pct": 100.0 * used / (used + avail) if used + avail else None,
        "inodes_used_pct": None,
    }
    if st.f_files:   # some filesystems (btrfs, NFS) report no inode limit
        out["inodes_used_pct"] = 100.0 * (st.f_files - st.f_ffree) / st.f_files
    return out


def dir_usage(root, deadline=None):
    """Allocated bytes and file count under root, like `du -sx`.

    Stays on root's filesystem, doesn't follow symlinks, counts hard links
    once. Returns dict(bytes, files, unreadable, complete); `complete` is
    False when `deadline` (time.monotonic) cut the walk short.
    """
    top = os.lstat(root)
    dev = top.st_dev
    total = top.st_blocks * 512
    files = unreadable = 0
    seen = set()
    stack = [root]
    while stack:
        if deadline is not None and time.monotonic() > deadline:
            return {"bytes": total, "files": files, "unreadable": unreadable, "complete": False}
        d = stack.pop()
        try:
            it = os.scandir(d)
        except OSError:
            unreadable += 1
            continue
        with it:
            for e in it:
                try:
                    st = e.stat(follow_symlinks=False)
                except OSError:
                    continue
                if st.st_dev != dev:          # another filesystem mounted below root
                    continue
                if stat.S_ISDIR(st.st_mode):
                    stack.append(e.path)
                else:
                    if st.st_nlink > 1:
                        key = (st.st_dev, st.st_ino)
                        if key in seen:
                            continue
                        seen.add(key)
                    files += 1
                total += st.st_blocks * 512
    return {"bytes": total, "files": files, "unreadable": unreadable, "complete": True}


class FolderScanner(threading.Thread):
    """Re-measures folder sizes every `interval` seconds in the background."""

    def __init__(self, paths, interval):
        threading.Thread.__init__(self, name="folder-scanner")
        self.daemon = True
        self.paths = list(paths)
        self.interval = interval
        self.results = {}          # path -> dir_usage() result + "at" (epoch)
        self.wake = threading.Event()
        self.stopped = False
        self._warned = set()

    def stop(self):
        self.stopped = True
        self.wake.set()

    def run(self):
        try:   # Linux: nice is per thread; I/O priority follows it under CFQ/BFQ
            os.setpriority(os.PRIO_PROCESS, 0, 19)
        except (AttributeError, OSError):
            pass
        while not self.stopped:
            for path in self.paths:
                if self.stopped:
                    break
                t0 = time.monotonic()
                try:
                    r = dir_usage(path)
                except OSError:
                    self.results.pop(path, None)   # folder vanished / not accessible
                    continue
                r["at"] = time.time()
                self.results[path] = r
                took = time.monotonic() - t0
                if r["unreadable"] and (path, "unreadable") not in self._warned:
                    self._warned.add((path, "unreadable"))
                    log.warning("folder size of %s: %d directories not readable as this user, "
                                "size is a lower bound", path, r["unreadable"])
                if took > max(30.0, self.interval / 2) and (path, "slow") not in self._warned:
                    self._warned.add((path, "slow"))
                    log.warning("measuring the size of %s took %.0fs; raise disk_scan_interval "
                                "or set it to 0 to record only filesystem usage", path, took)
            if self.wake.wait(self.interval):
                self.wake.clear()
