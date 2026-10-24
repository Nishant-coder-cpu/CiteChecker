#!/usr/bin/env bash
# Start the Verifier agent and the CiteCheck agent in the background (Linux/macOS).
# Usage: ./run.sh start | stop | restart | status | logs
cd "$(dirname "$0")"
ENV_AGENT_PORT=
ENV_VERIFIER_PORT=
if [ -f .env ]; then
  ENV_AGENT_PORT=$(sed -n 's/^AGENT_PORT=//p' .env | tail -1 | tr -d '[:space:]')
  ENV_VERIFIER_PORT=$(sed -n 's/^VERIFIER_PORT=//p' .env | tail -1 | tr -d '[:space:]')
fi

PY=${PY:-.venv/bin/python}
CITECHECK_PORT=${AGENT_PORT:-${ENV_AGENT_PORT:-8001}}
VERIFIER_PORT=${VERIFIER_PORT:-${ENV_VERIFIER_PORT:-8002}}
case "$CITECHECK_PORT:$VERIFIER_PORT" in
  *[!0-9:]*|:*|*:) echo "AGENT_PORT and VERIFIER_PORT must be numeric"; exit 1 ;;
esac
mkdir -p logs

# PID of this user's process listening on a port (empty if none)
port_pid() {
  ss -ltnpH "sport = :$1" 2>/dev/null | grep -o 'pid=[0-9]*' | head -1 | cut -d= -f2
}

running() {  # $1 = name, $2 = port
  local pid
  pid=$(port_pid "$2")
  [ -n "$pid" ] && echo "$pid" > "logs/$1.pid" && return 0
  return 1
}

start() {
  if running verifier "$VERIFIER_PORT" || running citecheck "$CITECHECK_PORT"; then
    echo "already running (use ./run.sh restart)"; status; return 1
  fi
  nohup "$PY" verifier_agent.py > logs/verifier.log 2>&1 & echo $! > logs/verifier.pid
  sleep 3
  nohup "$PY" agent.py > logs/citecheck.log 2>&1 & echo $! > logs/citecheck.pid
  sleep 10
  status
}

stop() {
  for entry in "citecheck:$CITECHECK_PORT" "verifier:$VERIFIER_PORT"; do
    name=${entry%%:*}; port=${entry##*:}
    pids="$(cat "logs/$name.pid" 2>/dev/null) $(port_pid "$port")"
    for pid in $(echo $pids | tr " " "\n" | sort -u); do kill "$pid" 2>/dev/null && echo "stopped $name (pid $pid)"; done
    rm -f "logs/$name.pid"
  done
  sleep 2
}

status() {
  for entry in "verifier:$VERIFIER_PORT" "citecheck:$CITECHECK_PORT"; do
    name=${entry%%:*}; port=${entry##*:}
    if running "$name" "$port"; then
      echo "$name: running (pid $(cat "logs/$name.pid"), port $port)"
      if [ "$name" = "citecheck" ]; then
        mailbox=$(grep -E "mailbox (server|client)" logs/citecheck.log 2>/dev/null | tail -1)
        [ -n "$mailbox" ] && echo "  mailbox: $mailbox"
      fi
    else echo "$name: stopped"; fi
  done
}

logs() {
  tail -n 80 -F logs/verifier.log logs/citecheck.log
}

case "${1:-start}" in
  start) start ;;
  stop) stop ;;
  restart) stop; start ;;
  status) status ;;
  logs) logs ;;
  *) echo "usage: $0 start|stop|restart|status|logs"; exit 1 ;;
esac
