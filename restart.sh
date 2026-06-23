#!/bin/bash
# Restart HandsOff (再买剁手).
cd "$(dirname "$0")"

bash stop.sh
# Give the old process a moment to release the web port + SQLite WAL lock.
sleep 1
bash start.sh
