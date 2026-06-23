#!/bin/bash
# Stop HandsOff (再买剁手). The [h] glob keeps pgrep/pkill from matching itself.
cd "$(dirname "$0")"

if pgrep -f "[h]andsoff.py" > /dev/null; then
  pkill -f "[h]andsoff.py"
  echo "HandsOff stopped."
else
  echo "HandsOff is not running."
fi
