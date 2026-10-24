#!/usr/bin/env bash
# Start the Verifier agent and the CiteCheck agent in the background (Linux/macOS).
# Usage: ./run.sh start | stop | status
cd "$(dirname "$0")"
PY=${PY:-.venv/bin/python}
mkdir -p logs

start() {
  nohup "$PY" verifier_agent.py > logs/verifier.log 2>&1 & echo $! > logs/verifier.pid
  sleep 3
  nohup "$PY" agent.py > logs/citecheck.log 2>&1 & echo $! > logs/citecheck.pid
  echo "started verifier (pid $(cat logs/verifier.pid)) and citecheck (pid $(cat logs/citecheck.pid))"
}
stop() {
  for n in citecheck verifier; do
    [ -f logs/$n.pid ] && kill "$(cat logs/$n.pid)" 2>/dev/null && echo "stopped $n"
    rm -f logs/$n.pid
  done
}
status() {
  for n in verifier citecheck; do
    if [ -f logs/$n.pid ] && kill -0 "$(cat logs/$n.pid)" 2>/dev/null; then echo "$n: running"; else echo "$n: stopped"; fi
  done
}
"${1:-start}"
