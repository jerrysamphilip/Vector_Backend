#!/usr/bin/env bash
# Start / stop N local API replicas against ONE load-test database (chunk V2).
#
#   scripts/load/replicas.sh start [N=2] [FIRST_PORT=8199]   # ports 8199, 8200, ...
#   scripts/load/replicas.sh status
#   scripts/load/replicas.sh stop                             # kills only the PIDs this script recorded
#
# Env: PYTHON (default python3), MYSQL_HOST/PORT/USER/PASSWORD, MYSQL_DATABASE (must start with vtest_load
# or load_), LOAD_PID_DIR (default reports/load/pids). Every replica gets the same JWT_SECRET_KEY and
# CREDENTIALS_ENCRYPTION_KEY, as every Kubernetes pod would from sales360-secret: a session cookie issued by
# one replica is then accepted by the other. RUN_BACKGROUND_JOBS=false (the worker is a separate process in
# deployments; the load test must not send mail). OPENAI_API_KEY is forced empty.
#
# Shared vs per-process state: the database (data, sessions, refresh-token rotation, idempotency) is shared;
# the rate-limit counters in app/core/rate_limit.py are in memory per process, so with N replicas a client
# gets N times the per-IP budget (GLOBAL_PER_IP 1200/min, sign-in 60 per 5 min) unless the load balancer
# pins it to one replica. run_load.py reports 429s per replica to make that visible.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PY="${PYTHON:-python3}"
PID_DIR="${LOAD_PID_DIR:-$ROOT/reports/load/pids}"
DB="${MYSQL_DATABASE:-}"

guard_db() {
  case "$DB" in
    outreach_ai) echo "refusing: $DB is a real application database" >&2; exit 2 ;;
    vtest_load*|load_*) ;;
    *) echo "refusing: MYSQL_DATABASE='$DB' must start with vtest_load or load_" >&2; exit 2 ;;
  esac
}

start() {
  local n="${1:-2}" first="${2:-8199}"
  guard_db
  mkdir -p "$PID_DIR"
  local secret="${JWT_SECRET_KEY:-v2-load-shared-jwt-secret-$(date +%Y%m%d)-0123456789abcdef}"
  local credkey="${CREDENTIALS_ENCRYPTION_KEY:-v2-load-shared-credentials-key}"
  for i in $(seq 0 $((n - 1))); do
    local port=$((first + i))
    if (echo > "/dev/tcp/127.0.0.1/$port") 2>/dev/null; then
      echo "port $port is already in use" >&2; exit 1
    fi
    cd "$ROOT"
    # nohup and env exec in place, so $! is the uvicorn process itself (stop kills exactly that PID)
    nohup env OPENAI_API_KEY= RUN_BACKGROUND_JOBS=false JWT_SECRET_KEY="$secret" \
      CREDENTIALS_ENCRYPTION_KEY="$credkey" LOG_LEVEL="${LOG_LEVEL:-WARNING}" \
      "$PY" -m uvicorn app.main:app --host 127.0.0.1 --port "$port" --workers 1 --no-access-log \
      > "$PID_DIR/api_$port.log" 2>&1 < /dev/null &
    echo $! > "$PID_DIR/api_$port.pid"
    echo "replica $((i + 1)): http://127.0.0.1:$port  pid $(cat "$PID_DIR/api_$port.pid")  log $PID_DIR/api_$port.log"
  done
  for i in $(seq 0 $((n - 1))); do
    local port=$((first + i)) ok=""
    for _ in $(seq 1 60); do
      if curl -fsS -m 2 "http://127.0.0.1:$port/health/ready" >/dev/null 2>&1; then ok=1; break; fi
      sleep 1
    done
    if [ -z "$ok" ]; then
      echo "replica on $port did not become ready; see $PID_DIR/api_$port.log" >&2
      tail -3 "$PID_DIR/api_$port.log" >&2 || true
      stop; exit 1
    fi
  done
  echo "all $n replicas ready"
}

status() {
  shopt -s nullglob
  for f in "$PID_DIR"/api_*.pid; do
    local pid; pid="$(cat "$f")"
    if kill -0 "$pid" 2>/dev/null; then echo "$(basename "$f" .pid): pid $pid running"; else echo "$(basename "$f" .pid): pid $pid gone"; fi
  done
}

stop() {
  shopt -s nullglob
  for f in "$PID_DIR"/api_*.pid; do
    local pid; pid="$(cat "$f")"
    if kill -0 "$pid" 2>/dev/null; then
      kill "$pid"
      # uvicorn drains in-flight requests on SIGTERM; give it 20 s, then SIGKILL that same PID
      for _ in $(seq 1 20); do kill -0 "$pid" 2>/dev/null || break; sleep 1; done
      if kill -0 "$pid" 2>/dev/null; then kill -9 "$pid"; echo "killed pid $pid (did not drain)"; fi
      echo "stopped pid $pid ($(basename "$f" .pid))"
    fi
    rm -f "$f"
  done
}

case "${1:-}" in
  start) shift; start "$@" ;;
  status) status ;;
  stop) stop ;;
  *) echo "usage: $0 start [N] [FIRST_PORT] | status | stop" >&2; exit 64 ;;
esac
