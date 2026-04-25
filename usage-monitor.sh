#!/bin/bash

set -u

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="$DIR/usage-monitor.pid"
LOG_FILE="$DIR/usage-monitor.log"

# Read host and port from .env
HOST="127.0.0.1"
PORT="8080"
if [ -f "$DIR/.env" ]; then
  while IFS='=' read -r key value; do
    case "$key" in
      PROXY_HOST) HOST="$value" ;;
      PROXY_PORT) PORT="$value" ;;
    esac
  done < "$DIR/.env"
fi

is_running() {
  if [ ! -f "$PID_FILE" ]; then
    return 1
  fi

  local pid
  pid="$(cat "$PID_FILE" 2>/dev/null)"

  if [ -z "$pid" ]; then
    return 1
  fi

  kill -0 "$pid" 2>/dev/null
}

case "${1:-}" in
  start)
    if is_running; then
      echo "usage-monitor is already running."
      exit 0
    fi

    rm -f "$PID_FILE"

    nohup uv run uvicorn usage_monitor.main:app --host "$HOST" --port "$PORT" >> "$LOG_FILE" 2>&1 &
    pid=$!
    echo "$pid" > "$PID_FILE"

    sleep 1
    if kill -0 "$pid" 2>/dev/null; then
      echo "usage-monitor started."
      echo "http://$HOST:$PORT"
    else
      echo "Failed to start usage-monitor. Check $LOG_FILE"
      rm -f "$PID_FILE"
      exit 1
    fi
    ;;

  stop)
    if is_running; then
      pid="$(cat "$PID_FILE")"
      kill "$pid" 2>/dev/null

      for _ in {1..10}; do
        if ! kill -0 "$pid" 2>/dev/null; then
          break
        fi
        sleep 1
      done

      if kill -0 "$pid" 2>/dev/null; then
        kill -9 "$pid" 2>/dev/null
      fi

      rm -f "$PID_FILE"
      echo "usage-monitor stopped."
    else
      rm -f "$PID_FILE"
      echo "usage-monitor is not running."
    fi
    ;;

  status)
    if is_running; then
      echo "running"
    else
      rm -f "$PID_FILE"
      echo "stopped"
    fi
    ;;

  restart)
    "$0" stop
    "$0" start
    ;;

  *)
    echo "Usage: $0 {start|stop|status|restart}"
    exit 1
    ;;
esac
