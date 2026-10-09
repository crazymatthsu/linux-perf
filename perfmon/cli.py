"""Command line entry point: python3 -m perfmon <command> ..."""

import argparse
import logging
import os
import re
import sys
import time

from . import __version__
from .config import Config, find_default

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def parse_duration(text):
    """'90' / '90s' / '15m' / '2h' / '1h30m' -> seconds."""
    if text is None:
        return None
    text = text.strip().lower()
    if re.match(r"^\d+(\.\d+)?$", text):
        return float(text)
    total = 0.0
    for num, unit in re.findall(r"(\d+(?:\.\d+)?)\s*([smhd])", text):
        total += float(num) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
    if total <= 0:
        raise argparse.ArgumentTypeError("bad duration: %r (use e.g. 90s, 15m, 2h)" % text)
    return total


def load_config(args):
    path = args.config or find_default(ROOT)
    cfg = Config(path)
    if getattr(args, "interval", None):
        cfg.interval = args.interval
    if getattr(args, "output_dir", None):
        cfg.output_dir = os.path.abspath(args.output_dir)
    return cfg


def resolve_run(cfg, ref):
    """Run directory from a path, a run id, 'latest', or part of a run name."""
    if ref and os.path.isdir(ref) and os.path.exists(os.path.join(ref, "meta.json")):
        return os.path.abspath(ref)
    base = cfg.output_dir
    if not ref or ref == "latest":
        link = os.path.join(base, "latest")
        if os.path.isdir(link):
            return os.path.realpath(link)
        runs = list_run_ids(base)
        if runs:
            return os.path.join(base, runs[-1])
        raise SystemExit("no recordings found in %s" % base)
    if os.path.isdir(os.path.join(base, ref)):
        return os.path.join(base, ref)
    hits = [r for r in list_run_ids(base) if ref in r]
    if hits:
        return os.path.join(base, hits[-1])
    raise SystemExit("run not found: %s (looked in %s)" % (ref, base))


def list_run_ids(base):
    if not os.path.isdir(base):
        return []
    return sorted(d for d in os.listdir(base)
                  if d != "latest" and os.path.exists(os.path.join(base, d, "meta.json")))


# --------------------------------------------------------------------- commands

def cmd_record(args):
    from .recorder import Recorder
    cfg = load_config(args)
    rec = Recorder(cfg, name=args.name)
    run_dir = rec.run(duration=args.duration)
    try:
        from .report import build_report
        build_report(run_dir)
    except Exception as e:  # the CSVs are what matter; never fail the run over the report
        logging.getLogger("perfmon").warning("could not write report.html: %s", e)
    print(run_dir)
    return 0


def cmd_discover(args):
    from .recorder import FILES, Recorder

    class Sink(object):
        def __init__(self):
            self.rows = []

        def write(self, row):
            self.rows.append(row)

        def flush(self):
            pass

    cfg = load_config(args)
    if not args.verbose:
        logging.getLogger("perfmon").setLevel(logging.WARNING)
    rec = Recorder(cfg)
    rec.discover()
    rec.logs = {k: Sink() for k in FILES}
    rec.sample(write=False)
    time.sleep(max(1.0, cfg.interval))
    rec.sample()
    print("config : %s" % (cfg.path or "(none - default: all processes in all containers)"))
    print("cgroup : %s    docker: %s" % (rec.cg.version, " ".join(cfg.docker_cmd)))
    print("targets: %s" % ", ".join(t.name for t in cfg.targets))
    print("")
    rows = rec.logs["containers"].rows
    print("CONTAINERS (%d)" % len(rows))
    _table(rows, [("container", "NAME", 28), ("image", "IMAGE", 36), ("cpu_pct", "CPU%", 7),
                  ("mem_used_mb", "MEM MB", 9), ("mem_limit_mb", "LIMIT MB", 9),
                  ("net_rx_mbps", "RX Mb/s", 8), ("pids", "PIDS", 5)])
    print("")
    rows = rec.logs["processes"].rows
    jvm = {r["series"]: r for r in rec.logs["jvm"].rows}
    for r in rows:
        j = jvm.get(r["series"])
        p = rec.procs.get(r["pid"])
        if j:
            r["jvm"] = "%.0f/%.0f MB" % (j["heap_used_mb"], j["heap_max_mb"] or 0)
        elif p is not None and p.java:
            r["jvm"] = "needs root" if p.perf_problem == "no-access" else "no hsperfdata"
        else:
            r["jvm"] = "-"
    print("PROCESSES (%d)" % len(rows))
    _table(rows, [("series", "SERIES", 40), ("target", "TARGET", 12), ("pid", "PID", 7),
                  ("cpu_pct", "CPU%", 7), ("rss_mb", "RSS MB", 9), ("threads", "THR", 5),
                  ("jvm", "HEAP used/max", 16)])
    if not rows:
        print("  (nothing matched - check [target ...] sections; run as root to see all processes)")
    if any(r["jvm"] == "needs root" for r in rows):
        print("\n  'needs root': that JVM runs as another user. Its CPU and memory are recorded, but heap/GC")
        print("  and open-file counts need perfmon to run as root or as the JVM's own user.")
    if any(r["jvm"] == "no hsperfdata" for r in rows):
        print("\n  'no hsperfdata': the JVM keeps no perf-data file (-XX:-UsePerfData, -XX:+PerfDisableSharedMem,")
        print("  or its uid has no name in the container's /etc/passwd). See README, JVM notes.")
    return 0


def _table(rows, cols):
    print("  " + " ".join(h.ljust(w) if i < 2 else h.rjust(w) for i, (_, h, w) in enumerate(cols)))
    for r in rows:
        cells = []
        for i, (k, _, w) in enumerate(cols):
            v = r.get(k)
            if isinstance(v, float):
                v = "%.1f" % v
            v = "" if v is None else str(v)
            cells.append(v[:w].ljust(w) if i < 2 else v[:w].rjust(w))
        print("  " + " ".join(cells))


def cmd_mark(args):
    from .recorder import add_marker
    cfg = load_config(args)
    run = resolve_run(cfg, args.run)
    add_marker(run, " ".join(args.text))
    print("marker added to %s" % os.path.basename(run))
    return 0


def cmd_list(args):
    import json
    cfg = load_config(args)
    ids = list_run_ids(cfg.output_dir)
    if not ids:
        print("no recordings in %s" % cfg.output_dir)
        return 0
    print("%-40s %-19s %10s %8s" % ("RUN", "STARTED", "DURATION", "STATUS"))
    for rid in ids:
        try:
            with open(os.path.join(cfg.output_dir, rid, "meta.json")) as f:
                m = json.load(f)
        except (OSError, ValueError):
            continue
        from .server import run_is_live
        live = run_is_live(os.path.join(cfg.output_dir, rid), m)
        end = m.get("ended") or time.time()
        dur = int(end - m["started"])
        print("%-40s %-19s %10s %8s" % (rid, time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(m["started"])),
                                        "%d:%02d:%02d" % (dur // 3600, dur // 60 % 60, dur % 60),
                                        "LIVE" if live else ("done" if m.get("ended") else "stopped")))
    return 0


def cmd_summary(args):
    from .report import print_summary
    cfg = load_config(args)
    print_summary(resolve_run(cfg, args.run))
    return 0


def cmd_report(args):
    from .report import build_report
    cfg = load_config(args)
    run = resolve_run(cfg, args.run)
    out = build_report(run, args.out, resample=args.resample)
    print(out)
    return 0


def cmd_serve(args):
    from .server import serve
    cfg = load_config(args)
    serve(cfg, bind=args.bind or cfg.web_bind, port=args.port or cfg.web_port,
          auth=args.auth or cfg.web_auth)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="perfmon", description="Record and chart CPU / memory / JVM "
                                 "metrics of Docker containers and the processes inside them.")
    ap.add_argument("-c", "--config", help="INI config file (default: ./perfmon.conf or $PERFMON_CONFIG)")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--version", action="version", version="perfmon " + __version__)
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("record", help="record metrics to CSV until Ctrl+C / SIGTERM / --duration")
    p.add_argument("-n", "--name", default="run", help="run name, e.g. 500-users")
    p.add_argument("-i", "--interval", type=float, help="seconds between samples (overrides config)")
    p.add_argument("-d", "--duration", type=parse_duration, help="stop after e.g. 30m, 2h")
    p.add_argument("-o", "--output-dir", help="where run directories are created")
    p.set_defaults(func=cmd_record)

    p = sub.add_parser("discover", help="dry run: show which containers/processes would be recorded")
    p.set_defaults(func=cmd_discover)

    p = sub.add_parser("serve", help="web dashboard: live + historical charts")
    p.add_argument("--bind", help="address to listen on (default from config: 0.0.0.0)")
    p.add_argument("--port", type=int, help="port (default from config: 8080)")
    p.add_argument("--auth", help="require HTTP basic auth, format user:password")
    p.add_argument("-o", "--output-dir", help="data directory with runs")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("mark", help="annotate the current run, e.g. 'ramp to 500 users'")
    p.add_argument("text", nargs="+")
    p.add_argument("--run", help="run id/name (default: latest)")
    p.set_defaults(func=cmd_mark)

    p = sub.add_parser("list", help="list recorded runs")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("summary", help="print avg / p95 / max per process and container")
    p.add_argument("run", nargs="?", help="run id/name/path (default: latest)")
    p.set_defaults(func=cmd_summary)

    p = sub.add_parser("report", help="write a self-contained HTML report (open it anywhere)")
    p.add_argument("run", nargs="?", help="run id/name/path (default: latest)")
    p.add_argument("-o", "--out", help="output file (default: <run>/report.html)")
    p.add_argument("--resample", type=parse_duration,
                   help="average into buckets of this size (e.g. 10s) to shrink long runs")
    p.set_defaults(func=cmd_report)

    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    if not getattr(args, "func", None):
        ap.print_help()
        return 1
    try:
        return args.func(args)
    except (IOError, ValueError) as e:
        print("error: %s" % e, file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
