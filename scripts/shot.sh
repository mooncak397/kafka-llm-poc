#!/usr/bin/env bash
# Headless screenshot of a Grafana kiosk URL. Usage: shot.sh URL OUT.png [HEIGHT]
set -eu
google-chrome-stable --headless=new --no-sandbox --disable-gpu --hide-scrollbars \
  --user-data-dir="$(mktemp -d)" --window-size=1600,"${3:-1400}" --virtual-time-budget=20000 \
  --screenshot="$2" "$1" >/dev/null 2>&1
