"""Self-contained HTML report and terminal summary for a recorded run."""

import collections
import csv
import io
import json
import math
import os
import time

from .recorder import FILES
from .server import WEB, read_meta

KEY = {"containers": "container", "processes": "series", "jvm": "series"}
TEXT = {"time", "epoch", "container", "image", "series", "target", "name", "kind", "text", "pid"}


def _read(path):
    if not os.path.exists(path):
        return ""
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


def _fmt(v):
    return ("%.2f" % v).rstrip("0").rstrip(".")


def resample_csv(text, key_col, secs):
    """Average rows into `secs`-wide buckets per series (counts/ms are summed)."""
    rows = list(csv.reader(io.StringIO(text)))
    if len(rows) < 2:
        return text
    header = rows[0]
    idx = {n: i for i, n in enumerate(header)}
    ep, tm = idx["epoch"], idx.get("time")
    kc = idx.get(key_col) if key_col else None
    numeric = [i for i, n in enumerate(header) if n not in TEXT]
    summed = {i for i in numeric if header[i].endswith("_count") or header[i].endswith("_ms")}
    buckets = collections.OrderedDict()
    for r in rows[1:]:
        if len(r) != len(header):
            continue
        try:
            t = float(r[ep])
        except ValueError:
            continue
        b = math.floor(t / secs) * secs
        k = (b, r[kc] if kc is not None else "")
        acc = buckets.get(k)
        if acc is None:
            acc = buckets[k] = [r, [0.0] * len(header), [0] * len(header)]
        acc[0] = r
        for i in numeric:
            if r[i] != "":
                try:
                    acc[1][i] += float(r[i])
                    acc[2][i] += 1
                except ValueError:
                    pass
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(header)
    for (b, _), (last, sums, counts) in buckets.items():
        row = list(last)
        for i in numeric:
            row[i] = _fmt(sums[i] if i in summed else sums[i] / counts[i]) if counts[i] else ""
        row[ep] = "%.3f" % b
        if tm is not None:
            row[tm] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(b))
        w.writerow(row)
    return out.getvalue()


def build_report_html(run_dir, resample=None):
    meta = read_meta(run_dir)
    meta["live"] = False
    files = {}
    for name in FILES:
        text = _read(os.path.join(run_dir, name + ".csv"))
        if resample and name != "markers" and text:
            text = resample_csv(text, KEY.get(name), resample)
        files[name] = text
    if resample:
        meta["interval"] = max(float(meta.get("interval") or 1), resample)
        meta["resampled"] = resample
    html = _read(os.path.join(WEB, "index.html"))
    embed = json.dumps({"meta": meta, "files": files}).replace("</", "<\\/")
    html = html.replace("<title>perfmon</title>",
                        "<title>perfmon report · %s</title>" % _esc(meta.get("name", "")))
    html = html.replace('<link rel="stylesheet" href="static/style.css">',
                        "<style>\n%s\n</style>" % _read(os.path.join(WEB, "style.css")))
    html = html.replace('<script src="static/chart.js"></script>',
                        "<script>\n%s\n</script>" % _read(os.path.join(WEB, "chart.js")))
    html = html.replace('<script src="static/app.js"></script>',
                        "<script>window.PERFMON_EMBED = %s;</script>\n<script>\n%s\n</script>"
                        % (embed, _read(os.path.join(WEB, "app.js"))))
    return html


def _esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build_report(run_dir, out=None, resample=None):
    out = out or os.path.join(run_dir, "report.html")
    html = build_report_html(run_dir, resample=resample)
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    return out


# ----------------------------------------------------------------- summary

def _rows(path):
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8", errors="replace") as f:
        return list(csv.DictReader(f))


def _num(v):
    try:
        return float(v) if v not in (None, "") else None
    except ValueError:
        return None


def _stats(vals):
    vals = [v for v in vals if v is not None]
    if not vals:
        return None
    s = sorted(vals)
    return {"n": len(vals), "avg": sum(vals) / len(vals), "p95": s[int(math.ceil(0.95 * len(s))) - 1],
            "max": s[-1], "first": vals[0], "last": vals[-1], "sum": sum(vals)}


def _group(rows, key):
    g = collections.OrderedDict()
    for r in rows:
        g.setdefault(r.get(key, ""), []).append(r)
    return g


def summarize(run_dir):
    procs = _group(_rows(os.path.join(run_dir, "processes.csv")), "series")
    jvms = _group(_rows(os.path.join(run_dir, "jvm.csv")), "series")
    ctrs = _group(_rows(os.path.join(run_dir, "containers.csv")), "container")
    host = _rows(os.path.join(run_dir, "host.csv"))
    col = lambda rows, c: [_num(r.get(c)) for r in rows]  # noqa: E731
    out = {"processes": [], "containers": [], "host": {}}
    for k, rows in procs.items():
        e = {"series": k, "cpu": _stats(col(rows, "cpu_pct")), "rss": _stats(col(rows, "rss_mb")),
             "threads": _stats(col(rows, "threads"))}
        j = jvms.get(k)
        if j:
            gc_ms = sum(sum(v for v in col(j, c) if v) for c in ("gc_young_ms", "gc_full_ms", "gc_other_ms"))
            e["jvm"] = {"heap": _stats(col(j, "heap_used_mb")), "heap_max": _stats(col(j, "heap_max_mb")),
                        "gc_ms": gc_ms, "gc_pct": _stats(col(j, "gc_pause_pct")),
                        "full_gcs": sum(v for v in col(j, "gc_full_count") if v)}
        out["processes"].append(e)
    for k, rows in ctrs.items():
        out["containers"].append({"container": k, "cpu": _stats(col(rows, "cpu_pct")),
                                  "mem": _stats(col(rows, "mem_used_mb")),
                                  "limit": _stats(col(rows, "mem_limit_mb")),
                                  "throttled": _stats(col(rows, "throttled_pct")),
                                  "rx": _stats(col(rows, "net_rx_mbps")),
                                  "tx": _stats(col(rows, "net_tx_mbps"))})
    for c in ("cpu_pct", "mem_used_mb", "load1", "collector_cpu_pct"):
        out["host"][c] = _stats(col(host, c))
    return out


def _f(st, key, digits=1):
    if not st or st.get(key) is None:
        return "-"
    return "%.*f" % (digits, st[key])


def _print_table(headers, rows):
    widths = [max(len(str(x)) for x in [h] + [r[i] for r in rows]) for i, h in enumerate(headers)]
    widths[0] = min(widths[0], 48)
    line = lambda cells: "  ".join(  # noqa: E731
        (str(c)[:widths[i]].ljust(widths[i]) if i == 0 else str(c).rjust(widths[i])) for i, c in enumerate(cells))
    print(line(headers))
    print("  ".join("-" * w for w in widths))
    for r in rows:
        print(line(r))


def print_summary(run_dir):
    meta = read_meta(run_dir)
    s = summarize(run_dir)
    end = meta.get("ended") or time.time()
    dur = int(end - meta["started"])
    print("Run      : %s  (%s)" % (meta["id"], "LIVE" if meta["live"] else "finished" if meta.get("ended") else "stopped"))
    print("Host     : %s, %s CPUs, %s MB RAM, cgroup %s" % (meta.get("host"), meta.get("cpus"),
                                                          meta.get("mem_total_mb"), meta.get("cgroup")))
    print("Started  : %s   duration %d:%02d:%02d   interval %ss" % (
        time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(meta["started"])),
        dur // 3600, dur // 60 % 60, dur % 60, meta.get("interval")))
    h = s["host"]
    print("Host CPU : avg %s%%  p95 %s%%  max %s%%   load1 max %s   mem used max %s MB   collector avg %s%%" % (
        _f(h["cpu_pct"], "avg"), _f(h["cpu_pct"], "p95"), _f(h["cpu_pct"], "max"),
        _f(h["load1"], "max", 2), _f(h["mem_used_mb"], "max", 0), _f(h["collector_cpu_pct"], "avg", 2)))
    print("")
    if s["processes"]:
        print("PROCESSES   (CPU: 100% = one core)")
        rows = []
        for e in s["processes"]:
            j = e.get("jvm") or {}
            rows.append([e["series"], _f(e["cpu"], "avg"), _f(e["cpu"], "p95"), _f(e["cpu"], "max"),
                         _f(e["rss"], "first", 0), _f(e["rss"], "last", 0), _f(e["rss"], "max", 0),
                         _f(e["threads"], "max", 0), _f(j.get("heap"), "max", 0), _f(j.get("heap_max"), "max", 0),
                         "%.1f" % (j["gc_ms"] / 1000.0) if j else "-", _f(j.get("gc_pct"), "max"),
                         "%d" % j["full_gcs"] if j else "-"])
        _print_table(["series", "cpu avg", "p95", "max", "rss MB start", "end", "max", "threads",
                      "heap max", "heap limit", "gc s", "gc% max", "full gc"], rows)
        print("")
    if s["containers"]:
        print("CONTAINERS")
        rows = [[e["container"], _f(e["cpu"], "avg"), _f(e["cpu"], "p95"), _f(e["cpu"], "max"),
                 _f(e["mem"], "avg", 0), _f(e["mem"], "max", 0), _f(e["limit"], "max", 0),
                 _f(e["throttled"], "max"), _f(e["rx"], "avg", 2), _f(e["tx"], "avg", 2)]
                for e in s["containers"]]
        _print_table(["container", "cpu avg", "p95", "max", "mem MB avg", "max", "limit",
                      "throttled% max", "rx Mb/s avg", "tx Mb/s avg"], rows)
