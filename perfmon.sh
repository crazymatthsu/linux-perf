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

CONF_ARGS=()
if [[ "${1:-}" == "-c" || "${1:-}" == "--config" ]]; then
  [[ -n "${2:-}" ]] || { echo "usage: $0 -c CONFIG <command> ..." >&2; exit 2; }
  CONF_ARGS=(-c "$(cd "$(dirname "$2")" && pwd)/$(basename "$2")")
  shift 2
elif [[ -n "${PERFMON_CONFIG:-}" ]]; then
  CONF_ARGS=(-c "$PERFMON_CONFIG")
elif [[ -f "$HERE/perfmon.conf" ]]; then
  CONF_ARGS=(-c "$HERE/perfmon.conf")
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
    echo "note: not running as root - JVM heap metrics, open-fd counts and docker access may be" >&2
    echo "      unavailable for processes owned by other users. Prefer: sudo $0 $*" >&2
  fi
}

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
  local name="${1:-run}"; [[ $# -gt 0 ]] && shift
  warn_root start
  mkdir -p "$STATE"
  nohup "$PY" -m perfmon ${CONF_ARGS[@]+"${CONF_ARGS[@]}"} record -n "$name" "$@" >"$STATE/recorder.out" 2>&1 &
  echo $! >"$REC_PID"
  sleep 2
  if ! alive "$REC_PID"; then
    echo "recorder failed to start:" >&2; tail -n 20 "$STATE/recorder.out" >&2; rm -f "$REC_PID"; exit 1
  fi
  echo "recording '$name' (pid $(cat "$REC_PID"))"
  echo "  data : $(latest_run)"
  echo "  log  : $STATE/recorder.out"
  alive "$WEB_PID" || echo "  live charts: $0 web"
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
  if alive "$REC_PID"; then
    echo "recorder : running (pid $(cat "$REC_PID")) -> $(latest_run)"
  else
    echo "recorder : stopped"
  fi
  if alive "$WEB_PID"; then
    echo "web      : running (pid $(cat "$WEB_PID"))"
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

Global: $0 -c CONFIG <command>   (default: ./perfmon.conf next to this script, if present)
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
