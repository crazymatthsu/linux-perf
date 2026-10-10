#!/usr/bin/env bash
# perfmon.sh - record CPU / memory / JVM metrics of Docker containers during a
# load test and watch them live in a browser.
#
#   ./perfmon.sh discover                 dry run: what would be recorded?
#   ./perfmon.sh start 500-users          start recording in the background
#   ./perfmon.sh web                      start the web dashboard (port 8080)
#   ./perfmon.sh mark "ramp to 1000 users"  annotate the charts
#   ./perfmon.sh stop                     stop recording, write report.html
#
# Run `./perfmon.sh help` for everything else.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE="$HERE/.state"
REC_PID="$STATE/recorder.pid"
WEB_PID="$STATE/web.pid"
export PYTHONPATH="$HERE${PYTHONPATH:+:$PYTHONPATH}"

OUT_FILE="$STATE/output_dir"   # data dir chosen by the most recent `start -o DIR`

abspath() { case "$1" in /*) echo "$1" ;; *) echo "$PWD/$1" ;; esac; }

# Global options, before the command: -c CONFIG, -o DATA_DIR (any order).
CONF_ARGS=()
OUT_SOURCE=""                  # option | env | remembered | "" (= config file / default)
[[ -n "${PERFMON_OUTPUT_DIR:-}" ]] && OUT_SOURCE=env
while [[ $# -gt 0 ]]; do
  case "$1" in
    -c|--config)
      [[ -n "${2:-}" ]] || { echo "usage: $0 -c CONFIG <command> ..." >&2; exit 2; }
      CONF_ARGS=(-c "$(cd "$(dirname "$2")" && pwd)/$(basename "$2")"); shift 2 ;;
    -o|--output-dir)
      [[ -n "${2:-}" ]] || { echo "usage: $0 -o DATA_DIR <command> ..." >&2; exit 2; }
      export PERFMON_OUTPUT_DIR="$(abspath "$2")"; OUT_SOURCE=option; shift 2 ;;
    *) break ;;
  esac
done
if [[ ${#CONF_ARGS[@]} -eq 0 ]]; then
  if [[ -n "${PERFMON_CONFIG:-}" ]]; then
    CONF_ARGS=(-c "$PERFMON_CONFIG")
  elif [[ -f "$HERE/perfmon.conf" ]]; then
    CONF_ARGS=(-c "$HERE/perfmon.conf")
  fi
fi
# Until the next `start`, every command follows the data dir of the last `start -o`.
if [[ -z "$OUT_SOURCE" && -s "$OUT_FILE" ]]; then
  export PERFMON_OUTPUT_DIR="$(cat "$OUT_FILE")"; OUT_SOURCE=remembered
fi

find_python() {
  local p
  for p in "${PYTHON:-}" python3 /usr/libexec/platform-python python3.12 python3.11 python3.10 \
           python3.9 python3.8 python3.7 python3.6; do
    [[ -n "$p" ]] || continue
    if command -v "$p" >/dev/null 2>&1 &&
       "$p" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 6) else 1)' 2>/dev/null; then
      echo "$p"; return 0
    fi
  done
  echo "ERROR: Python 3.6+ not found. Install python3 or set PYTHON=/path/to/python3" >&2
  exit 1
}
PY="$(find_python)"

pm() { "$PY" -m perfmon ${CONF_ARGS[@]+"${CONF_ARGS[@]}"} "$@"; }

alive() { [[ -f "$1" ]] && kill -0 "$(cat "$1")" 2>/dev/null; }

warn_root() {
  if [[ "$(id -u)" != "0" ]]; then
    echo "note: running as $(id -un), not root. CPU, memory, threads and container metrics are recorded" >&2
    echo "      for every process; JVM heap/GC and open-file counts only for processes running as" >&2
    echo "      $(id -un) (check with: ps -eo user,pid,comm | grep -E 'java|ampServer')." >&2
  fi
}

data_dir() {
  "$PY" - ${CONF_ARGS[@]+"${CONF_ARGS[@]}"} <<'PYEOF'
import argparse, sys
from perfmon.cli import load_config
print(load_config(argparse.Namespace(config=sys.argv[2] if len(sys.argv) > 2 else None)).output_dir)
PYEOF
}

web_dir() { sed -n 's/^perfmon dashboard serving //p' "$STATE/web.out" 2>/dev/null | head -n 1; }

latest_run() {
  "$PY" - ${CONF_ARGS[@]+"${CONF_ARGS[@]}"} <<'EOF'
import sys
from perfmon.cli import load_config, resolve_run
import argparse
a = argparse.Namespace(config=sys.argv[2] if len(sys.argv) > 2 else None)
try:
    print(resolve_run(load_config(a), "latest"))
except SystemExit:
    pass
EOF
}

cmd_start() {
  if alive "$REC_PID"; then
    echo "already recording (pid $(cat "$REC_PID")) -> $(latest_run)"; exit 1
  fi
  local name="run" args=()
  if [[ $# -gt 0 && "$1" != -* ]]; then name="$1"; shift; fi
  # -o DIR after the name: handled here so later commands find the same run.
  while [[ $# -gt 0 ]]; do
    case "$1" in
      -o|--output-dir)
        [[ -n "${2:-}" ]] || { echo "-o needs a directory" >&2; exit 2; }
        export PERFMON_OUTPUT_DIR="$(abspath "$2")"; OUT_SOURCE=option; shift 2 ;;
      --output-dir=*) export PERFMON_OUTPUT_DIR="$(abspath "${1#*=}")"; OUT_SOURCE=option; shift ;;
      *) args+=("$1"); shift ;;
    esac
  done
  if [[ "$OUT_SOURCE" == remembered ]]; then   # a plain `start` goes back to the config file
    unset PERFMON_OUTPUT_DIR; OUT_SOURCE=""
  fi
  warn_root start
  mkdir -p "$STATE"
  if [[ -n "$OUT_SOURCE" ]]; then echo "$PERFMON_OUTPUT_DIR" >"$OUT_FILE"; else rm -f "$OUT_FILE"; fi
  nohup "$PY" -m perfmon ${CONF_ARGS[@]+"${CONF_ARGS[@]}"} record -n "$name" ${args[@]+"${args[@]}"} \
    >"$STATE/recorder.out" 2>&1 &
  echo $! >"$REC_PID"
  sleep 2
  if ! alive "$REC_PID"; then
    echo "recorder failed to start:" >&2; tail -n 20 "$STATE/recorder.out" >&2; rm -f "$REC_PID"; exit 1
  fi
  echo "recording '$name' (pid $(cat "$REC_PID"))"
  echo "  data : $(latest_run)"
  echo "  log  : $STATE/recorder.out"
  if ! alive "$WEB_PID"; then
    echo "  live charts: $0 web"
  elif [[ "$(web_dir)" != "$(data_dir)" ]]; then
    echo "  note: the web dashboard is showing $(web_dir); restart it to see this run:"
    echo "        $0 web-stop && $0 web"
  fi
}

cmd_stop() {
  if ! alive "$REC_PID"; then
    echo "not recording"; rm -f "$REC_PID"; return 0
  fi
  local pid; pid="$(cat "$REC_PID")"
  kill -TERM "$pid"
  for _ in $(seq 1 30); do kill -0 "$pid" 2>/dev/null || break; sleep 0.5; done
  kill -0 "$pid" 2>/dev/null && { echo "recorder did not stop, killing" >&2; kill -KILL "$pid"; }
  rm -f "$REC_PID"
  local run; run="$(latest_run)"
  echo "stopped. run: $run"
  if [[ -n "$run" ]]; then
    [[ -f "$run/report.html" ]] || pm report "$run" >/dev/null
    echo "report: $run/report.html  (self-contained, open in any browser)"
    echo "summary: $0 summary"
  fi
}

cmd_status() {
  local src="config file"
  case "$OUT_SOURCE" in
    option) src="-o option" ;; env) src="\$PERFMON_OUTPUT_DIR" ;; remembered) src="from the last start -o" ;;
  esac
  echo "data     : $(data_dir)  ($src)"
  if alive "$REC_PID"; then
    echo "recorder : running (pid $(cat "$REC_PID")) -> $(latest_run)"
  else
    echo "recorder : stopped"
  fi
  if alive "$WEB_PID"; then
    echo "web      : running (pid $(cat "$WEB_PID")), showing $(web_dir)"
    grep -E "http://" "$STATE/web.out" 2>/dev/null | head -n 4 | sed 's/^/           /'
  else
    echo "web      : stopped"
  fi
}

cmd_web() {
  if alive "$WEB_PID"; then
    echo "web dashboard already running (pid $(cat "$WEB_PID"))"; grep -E "http://" "$STATE/web.out" | head -n 4; exit 0
  fi
  mkdir -p "$STATE"
  local args=("$@")
  [[ $# -eq 1 && "$1" =~ ^[0-9]+$ ]] && args=(--port "$1")
  nohup "$PY" -m perfmon ${CONF_ARGS[@]+"${CONF_ARGS[@]}"} serve ${args[@]+"${args[@]}"} >"$STATE/web.out" 2>&1 &
  echo $! >"$WEB_PID"
  sleep 1.5
  if ! alive "$WEB_PID"; then
    echo "web server failed to start:" >&2; cat "$STATE/web.out" >&2; rm -f "$WEB_PID"; exit 1
  fi
  cat "$STATE/web.out"
  echo "(running in background, pid $(cat "$WEB_PID"); stop with: $0 web-stop)"
}

cmd_web_stop() {
  if alive "$WEB_PID"; then kill -TERM "$(cat "$WEB_PID")"; echo "web dashboard stopped"; else echo "web dashboard not running"; fi
  rm -f "$WEB_PID"
}

# Record around a load-test command: start, mark, run it, cool down, stop.
cmd_wrap() {
  local name="${1:?usage: $0 wrap NAME [--cooldown SECS] -- command args...}"; shift
  local cooldown=10
  if [[ "${1:-}" == "--cooldown" ]]; then cooldown="$2"; shift 2; fi
  [[ "${1:-}" == "--" ]] && shift
  [[ $# -gt 0 ]] || { echo "usage: $0 wrap NAME [--cooldown SECS] -- command args..." >&2; exit 2; }
  cmd_start "$name"
  sleep "${WARMUP:-5}"
  pm mark "load test start: $*"
  set +e; "$@"; local rc=$?; set -e
  pm mark "load test end (exit code $rc)"
  echo "load test finished (exit $rc); cooling down ${cooldown}s"
  sleep "$cooldown"
  cmd_stop
  return "$rc"
}

usage() {
  cat <<EOF
perfmon - container / process / JVM performance recorder

Recording
  $0 discover                     dry run: list containers & processes that would be recorded
  $0 start [NAME] [-i SECS] [-d DURATION]
                                  start recording in the background (e.g. -i 2 -d 1h)
  $0 mark "TEXT"                  add a marker to the live charts (e.g. "ramp to 500 users")
  $0 stop                         stop recording and write <run>/report.html
  $0 wrap NAME -- CMD ARGS...     record while CMD runs (adds start/end markers, then stops)
  $0 status

Viewing
  $0 web [PORT]                   start the web dashboard in the background (default 8080)
  $0 web-stop
  $0 list                         list recorded runs
  $0 summary [RUN]                avg / p95 / max per process and container
  $0 report [RUN] [--resample 10s]  self-contained HTML report

Foreground / advanced
  $0 record [-n NAME] [-i SECS] [-d DURATION]
  $0 serve [--bind ADDR] [--port N] [--auth user:pass]

Global options (before the command):
  -c CONFIG     config file (default: perfmon.conf next to this script, or \$PERFMON_CONFIG)
  -o DATA_DIR   where recordings are written and read (default: output_dir in the config,
                or \$PERFMON_OUTPUT_DIR). "start NAME -o DIR" works too; later commands
                then use DIR until the next start.
EOF
}

cmd="${1:-help}"; [[ $# -gt 0 ]] && shift
case "$cmd" in
  start)    cmd_start "$@" ;;
  stop)     cmd_stop ;;
  status)   cmd_status ;;
  web)      cmd_web "$@" ;;
  web-stop) cmd_web_stop ;;
  wrap)     cmd_wrap "$@" ;;
  discover) warn_root discover; pm discover "$@" ;;
  mark|list|summary|report|record|serve) pm "$cmd" "$@" ;;
  help|-h|--help) usage ;;
  *) echo "unknown command: $cmd" >&2; usage; exit 2 ;;
esac
