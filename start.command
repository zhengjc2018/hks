#!/bin/zsh
set -e
cd "$(dirname "$0")"

PY=".venv/bin/python"
PORT="${APANEL_PORT:-}"
MODE="start"

is_free() {
  ! lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1
}

is_apanel() {
  curl -s --max-time 1 "http://127.0.0.1:$1/api/health" 2>/dev/null | grep -q '"ok":true'
}

if [ -z "$PORT" ]; then
  for candidate in 5000 5050 5001 5051; do
    if is_free "$candidate"; then
      PORT="$candidate"
      MODE="start"
      break
    fi
    if is_apanel "$candidate"; then
      PORT="$candidate"
      MODE="running"
      break
    fi
  done
  if [ -z "$PORT" ]; then
    echo "No free port found in 5000/5050/5001/5051."
    exit 1
  fi
fi

if [ ! -x "$PY" ]; then
  echo "Creating Python virtual environment..."
  python3 -m venv .venv
fi

if ! "$PY" -c "import flask, requests, easy_tdx" >/dev/null 2>&1; then
  echo "Installing dependencies (first run only)..."
  "$PY" -m pip install -r requirements.lock.txt
fi

if [ ! -f config.json ]; then
  cp config.example.json config.json
  echo "Created config.json from template."
fi

if [ "$MODE" = "running" ] || is_apanel "$PORT"; then
  echo "Already running at http://127.0.0.1:$PORT/"
  open "http://127.0.0.1:$PORT/"
  exit 0
fi

mkdir -p logs
export APANEL_PORT="$PORT"
export APANEL_HOST="127.0.0.1"
export APANEL_LLM_MOCK=0
nohup "$PY" server.py > logs/server.log 2>&1 &
echo $! > logs/server.pid

echo "Starting A股机会雷达 at http://127.0.0.1:$PORT/"
for _ in {1..30}; do
  if curl -s -o /dev/null --max-time 1 "http://127.0.0.1:$PORT/" >/dev/null 2>&1; then
    open "http://127.0.0.1:$PORT/"
    echo "Ready."
    exit 0
  fi
  sleep 0.5
done

echo "Server did not respond in time; check logs/server.log:"
tail -20 logs/server.log
exit 1
