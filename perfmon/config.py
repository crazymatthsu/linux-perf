"""INI configuration: global settings plus [target <name>] sections."""

import configparser
import os
import re

from .disks import parse_paths

INSTALL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DEFAULTS = {
    "interval": "1",
    "discover_interval": "10",
    "output_dir": "data",
    "docker_cmd": "docker",
    "containers": ".*",
    "host_metrics": "yes",
    "disk_paths": "/logs, /apps",
    "disk_scan_interval": "60",
    "web_bind": "0.0.0.0",
    "web_port": "8080",
    "web_auth": "",
}

# Used when the config has no [target ...] sections: every process in every
# running container, minus init/shell wrappers that only add noise.
DEFAULT_TARGET = {
    "container": ".*",
    "process": ".*",
    "exclude": r"^(\S*/)?(tini|dumb-init|docker-init|s6-\S+|sleep|tail|cat|sh|bash|su|sudo|gosu)( |$)",
    "jvm": "auto",
}


class Target(object):
    def __init__(self, name, sec):
        self.name = name
        self.container = _rx(sec.get("container"))
        self.image = _rx(sec.get("image"))
        self.exe = _rx(sec.get("exe"))
        self.process = _rx(sec.get("process"))
        self.exclude = _rx(sec.get("exclude"))
        self.label = (sec.get("label") or "").strip() or None
        jvm = (sec.get("jvm") or "auto").strip().lower()
        if jvm not in ("auto", "yes", "no", "true", "false", "on", "off", "1", "0"):
            raise ValueError("target %s: jvm must be auto/yes/no" % name)
        self.jvm = {"true": "yes", "on": "yes", "1": "yes",
                    "false": "no", "off": "no", "0": "no"}.get(jvm, jvm)
        # A target without container/image matches processes anywhere on the host.
        self.needs_container = self.container is not None or self.image is not None

    def matches_container(self, name, image):
        if not self.needs_container:
            return True
        if name is None:
            return False
        if self.container is not None and not self.container.search(name):
            return False
        if self.image is not None and not self.image.search(image or ""):
            return False
        return True

    def matches_process(self, exe, cmdline):
        if self.exe is not None and not self.exe.fullmatch(exe):
            return False
        if self.process is not None and not self.process.search(cmdline):
            return False
        if self.exclude is not None and self.exclude.search(cmdline):
            return False
        return True

    def describe(self):
        def p(r):
            return r.pattern if r is not None else None
        return {"name": self.name, "container": p(self.container), "image": p(self.image),
                "exe": p(self.exe), "process": p(self.process), "exclude": p(self.exclude),
                "label": self.label, "jvm": self.jvm}


def _rx(value):
    if value is None or not value.strip():
        return None
    return re.compile(value.strip())


class Config(object):
    def __init__(self, path=None):
        cp = configparser.ConfigParser(inline_comment_prefixes=(";", "#"), interpolation=None)
        self.path = None
        if path:
            if not os.path.exists(path):
                raise IOError("config file not found: %s" % path)
            cp.read(path)
            self.path = os.path.abspath(path)
        g = dict(DEFAULTS)
        if cp.has_section("perfmon"):
            g.update(cp["perfmon"])
        self.set_interval(g["interval"])
        self.discover_interval = float(g["discover_interval"])
        # Relative paths are relative to the config file, else to the install dir.
        base = os.path.dirname(self.path) if self.path else INSTALL_DIR
        # Where runs are written: $PERFMON_OUTPUT_DIR beats the config file.
        env_out = os.environ.get("PERFMON_OUTPUT_DIR", "").strip()
        if env_out:
            self.output_dir = os.path.abspath(os.path.expanduser(env_out))
        else:
            self.output_dir = os.path.normpath(os.path.join(base, os.path.expanduser(g["output_dir"])))
        self.docker_cmd = g["docker_cmd"].split()
        self.containers = re.compile(g["containers"] or ".*")
        self.host_metrics = cp.BOOLEAN_STATES.get(g["host_metrics"].lower(), True)
        self.disk_paths = parse_paths(g["disk_paths"])
        self.disk_scan_interval = float(g["disk_scan_interval"] or 0)
        self.web_bind = g["web_bind"]
        self.web_port = int(g["web_port"])
        self.web_auth = g["web_auth"].strip() or None
        self.targets = []
        for sec in cp.sections():
            m = re.match(r"^target\s+(\S.*)$", sec)
            if m:
                self.targets.append(Target(m.group(1).strip(), cp[sec]))
        self.default_targets = not self.targets
        if self.default_targets:
            self.targets.append(Target("containers", DEFAULT_TARGET))


    def set_interval(self, value):
        try:
            v = float(value)
        except (TypeError, ValueError):
            raise ValueError("interval must be a number of seconds, got %r" % (value,))
        if not 0.2 <= v <= 3600:
            raise ValueError("interval must be between 0.2 and 3600 seconds, got %g" % v)
        self.interval = v


def find_default(script_dir):
    """perfmon.conf next to the scripts, or $PERFMON_CONFIG."""
    env = os.environ.get("PERFMON_CONFIG")
    if env:
        return env
    for cand in (os.path.join(os.getcwd(), "perfmon.conf"), os.path.join(script_dir, "perfmon.conf")):
        if os.path.exists(cand):
            return cand
    return None
