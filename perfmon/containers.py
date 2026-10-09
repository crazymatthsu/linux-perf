"""Running-container discovery through the docker (or podman) CLI."""

import logging
import subprocess
import threading

log = logging.getLogger("perfmon")


class DockerError(Exception):
    pass


def list_containers(docker_cmd):
    """[{id, name, image}] for running containers (full 64-char ids)."""
    cmd = list(docker_cmd) + ["ps", "--no-trunc", "--format", "{{.ID}}\t{{.Names}}\t{{.Image}}"]
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             universal_newlines=True)
        out, err = p.communicate(timeout=20)
    except OSError as e:
        raise DockerError("cannot run %s: %s" % (" ".join(cmd[:1]), e))
    except subprocess.TimeoutExpired:
        p.kill()
        p.communicate()
        raise DockerError("'%s ps' timed out" % cmd[0])
    if p.returncode != 0:
        raise DockerError((err or out).strip().splitlines()[-1] if (err or out).strip()
                          else "'%s ps' failed" % cmd[0])
    result = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 3 and len(parts[0]) >= 12:
            result.append({"id": parts[0].strip(), "name": parts[1].split(",")[0].strip(),
                           "image": parts[2].strip()})
    return result


class ContainerWatcher(threading.Thread):
    """Refreshes the container list in the background.

    `docker ps` can take seconds on a loaded host; doing it here keeps the
    sampling loop on time. A failed refresh keeps the last good snapshot so a
    hiccup of the daemon doesn't look like every container stopping.
    """

    def __init__(self, docker_cmd, interval):
        threading.Thread.__init__(self, name="container-watcher")
        self.daemon = True
        self.docker_cmd = docker_cmd
        self.interval = interval
        self.snapshot = {}
        self.generation = 0   # bumped whenever the set of containers changes
        self.ready = threading.Event()
        self.wake = threading.Event()
        self.stopped = False

    def refresh_soon(self):
        self.wake.set()

    def stop(self):
        self.stopped = True
        self.wake.set()

    def run(self):
        warned = False
        while not self.stopped:
            try:
                snap = {c["id"]: c for c in list_containers(self.docker_cmd)}
                if set(snap) != set(self.snapshot):
                    self.generation += 1
                self.snapshot = snap
                if warned:
                    log.info("container discovery working again")
                warned = False
            except DockerError as e:
                if not warned:
                    log.warning("container discovery failed: %s (is docker running / do you need sudo?)", e)
                    warned = True
            self.ready.set()
            if self.wake.wait(self.interval):
                self.wake.clear()
