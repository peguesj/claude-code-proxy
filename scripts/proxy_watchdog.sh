#!/usr/bin/env bash
# CCP LiteLLM proxy watchdog for 127.0.0.1:8082 (launchd label com.ccp.litellm.proxy).
#
# Swap protocol (see apm/ markers):
#   touch apm/.swap-lock   before any manual restart; watchdog stays hands-off while it is fresh (<15 min)
#   touch apm/.swap-ready  to have the watchdog perform the kickstart swap itself
#   rm apm/.swap-lock      after the manual swap is finished
#
# Logs: JSON lines {ts,event,detail} appended to apm/watchdog.jsonl on state changes and restarts only.
# Never reads dotenv files and never prints response bodies or secrets.

set -u

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APM="$REPO/apm"
LABEL="com.ccp.litellm.proxy"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
DOMAIN="gui/$(id -u)"
TARGET="${DOMAIN}/${LABEL}"
HOST="127.0.0.1"
PORT="8082"
HEALTH_URL="http://${HOST}:${PORT}/ccp/health"
INTERVAL=10
FAIL_THRESHOLD=3
LOCK_STALE_SECS=900
SWAP_WAIT_SECS=60
RESTART_WAIT_SECS=60

LOCK="$APM/.swap-lock"
READY="$APM/.swap-ready"
LOG="$APM/watchdog.jsonl"
PIDFILE="$APM/.watchdog.pid"

STATE=""
FAILS=0
STALE_NOTED=""

ts() { date -u +%Y-%m-%dT%H:%M:%SZ; }

json_str() {
  printf '%s' "$1" | tr '\n\r\t' '   ' | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' | tr -d '\n'
}

log_event() { # event detail
  printf '{"ts":"%s","event":"%s","detail":"%s"}\n' "$(ts)" "$1" "$(json_str "$2")" >> "$LOG"
}

set_state() { # name detail -- logs only when the state name changes
  if [ "$1" != "$STATE" ]; then
    STATE="$1"
    log_event "state" "$1: $2"
  fi
}

healthy() { curl -sf -m3 "$HEALTH_URL" >/dev/null 2>&1; }

listener_pid() {
  lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -Fp 2>/dev/null | sed -n 's/^p//p' | head -1
}

listener_ok() { # LISTEN socket must match the expected bind (default * = LAN-reachable)
  local names
  names="$(lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -Fn 2>/dev/null | grep '^n' | sort -u)"
  [ -n "$names" ] && [ "$names" = "n${CCP_EXPECT_BIND:-*}:${PORT}" ]
}

job_loaded() { launchctl print "$TARGET" >/dev/null 2>&1; }

wait_healthy() { # timeout seconds
  local end=$(( $(date +%s) + $1 ))
  while [ "$(date +%s)" -lt "$end" ]; do
    if healthy; then return 0; fi
    sleep 2
  done
  return 1
}

restart_proxy() { # reason
  local out rc
  log_event "restart_begin" "$1"
  if ! job_loaded; then
    log_event "job_not_loaded" "bootstrapping $PLIST"
    out="$(launchctl bootstrap "$DOMAIN" "$PLIST" 2>&1)"; rc=$?
    log_event "bootstrap" "rc=$rc ${out}"
  fi
  # -k so a hung-but-running process is actually replaced
  out="$(launchctl kickstart -k "$TARGET" 2>&1)"; rc=$?
  log_event "kickstart" "rc=$rc ${out}"
  if wait_healthy "$RESTART_WAIT_SECS" && listener_ok; then
    log_event "restart_result" "healthy pid=$(listener_pid)"
  else
    log_event "restart_result" "NOT healthy after ${RESTART_WAIT_SECS}s listener_pid=$(listener_pid)"
  fi
}

do_swap() {
  local before after out rc ok
  before="$(listener_pid)"
  log_event "swap_begin" "swap-ready marker present; pid_before=${before:-none}"
  out="$(launchctl kickstart -k "$TARGET" 2>&1)"; rc=$?
  sleep 2  # give the old process time to exit so we do not pass on its health
  ok="false"
  if wait_healthy "$SWAP_WAIT_SECS" && listener_ok; then ok="true"; fi
  after="$(listener_pid)"
  log_event "swap_result" "ok=$ok kickstart_rc=$rc pid_before=${before:-none} pid_after=${after:-none} listener_ok=$(listener_ok && echo true || echo false) ${out}"
  rm -f "$READY"
  STATE=""  # force a state line after the swap
}

cleanup() {
  if [ -f "$PIDFILE" ] && [ "$(cat "$PIDFILE" 2>/dev/null)" = "$$" ]; then
    rm -f "$PIDFILE"
  fi
}

main() {
  mkdir -p "$APM"

  if [ -f "$PIDFILE" ]; then
    local oldpid
    oldpid="$(cat "$PIDFILE" 2>/dev/null)"
    if [ -n "$oldpid" ] && kill -0 "$oldpid" 2>/dev/null \
       && ps -p "$oldpid" -o command= 2>/dev/null | grep -q 'proxy_watchdog'; then
      echo "proxy_watchdog already running (pid $oldpid); refusing to start a second copy" >&2
      exit 1
    fi
  fi

  echo "$$" > "$PIDFILE"
  trap cleanup EXIT
  trap 'exit 0' INT TERM
  log_event "watchdog_start" "pid=$$ target=$TARGET health=$HEALTH_URL"

  while true; do
    # 1. Swap lock: stay hands-off while fresh. Stale locks are ignored and logged once.
    if [ -e "$LOCK" ]; then
      local now mtime age
      now="$(date +%s)"
      mtime="$(stat -f %m "$LOCK" 2>/dev/null || echo "$now")"
      age=$(( now - mtime ))
      if [ "$age" -lt "$LOCK_STALE_SECS" ]; then
        set_state "swap_locked" "swap-lock age ${age}s; watchdog hands-off"
        sleep "$INTERVAL"
        continue
      fi
      if [ "$STALE_NOTED" != "$mtime" ]; then
        log_event "swap_lock_stale" "ignoring .swap-lock age ${age}s (> ${LOCK_STALE_SECS}s)"
        STALE_NOTED="$mtime"
      fi
    fi

    # 2. Watchdog-performed swap requested
    if [ -e "$READY" ]; then
      do_swap
    fi

    # 3. Health monitoring and keep-alive
    if healthy; then
      FAILS=0
      set_state "up" "health ok at $HEALTH_URL"
    else
      FAILS=$(( FAILS + 1 ))
      set_state "down" "health check failed (consecutive=$FAILS)"
      if [ "$FAILS" -ge "$FAIL_THRESHOLD" ]; then
        restart_proxy "$FAILS consecutive health failures"
        FAILS=0
      fi
    fi

    sleep "$INTERVAL"
  done
}

main "$@"
