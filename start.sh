#!/bin/bash
# Start HandsOff (再买剁手) in the background.
set -e
cd "$(dirname "$0")"

# Prefer a local virtualenv if present, else fall back to python3.
if [ -x "venv/bin/python" ]; then
  PYTHON="venv/bin/python"
elif [ -x ".venv/bin/python" ]; then
  PYTHON=".venv/bin/python"
else
  PYTHON="${PYTHON:-python3}"
fi

if pgrep -f "[h]andsoff.py" > /dev/null; then
  echo "HandsOff is already running (pid $(pgrep -f '[h]andsoff.py' | tr '\n' ' '))."
  exit 0
fi

nohup "$PYTHON" handsoff.py >> handsoff.log 2>&1 &
echo "HandsOff started (pid $!). Logs: handsoff.log"
