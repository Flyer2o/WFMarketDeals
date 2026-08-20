#!/bin/bash
# Run the scanner continuously in the background (starts at login, restarts if
# it crashes). Without this the app only runs while a terminal window is open,
# which silently disables everything that depends on watching over time:
# dip detection, Baro arrival alerts, Twitch auto-open, full market sweeps.
#
#   ./autostart.sh install    turn it on
#   ./autostart.sh stop       turn it off, keep it installed
#   ./autostart.sh uninstall  remove it completely
#   ./autostart.sh status     is it running?
#   ./autostart.sh log        tail the log

set -euo pipefail
APPDIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LABEL="com.kris.wfm-scanner"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
PYTHON="$(command -v python3)"

case "${1:-status}" in
  install)
    mkdir -p "$HOME/Library/LaunchAgents"
    sed -e "s|__PYTHON__|$PYTHON|g" -e "s|__APPDIR__|$APPDIR|g" \
        "$APPDIR/$LABEL.plist" > "$PLIST"
    launchctl unload "$PLIST" 2>/dev/null || true
    launchctl load "$PLIST"
    echo "installed — scanner runs at login and restarts if it dies"
    echo "  UI:   http://127.0.0.1:8737"
    echo "  log:  $APPDIR/scanner.log"
    echo "  off:  $APPDIR/autostart.sh uninstall"
    ;;
  stop)
    launchctl unload "$PLIST" 2>/dev/null && echo "stopped (still installed)" \
      || echo "was not running"
    ;;
  uninstall)
    launchctl unload "$PLIST" 2>/dev/null || true
    rm -f "$PLIST"
    echo "removed — it will not start again"
    ;;
  status)
    # note: `launchctl list | grep -q` would report a false negative here —
    # grep -q exits on the first match, launchctl takes SIGPIPE, and pipefail
    # turns that into a non-zero pipeline
    entry="$(launchctl list | awk -v l="$LABEL" '$3 == l')"
    if [ -n "$entry" ]; then
      echo "running:"
      echo "$entry" | awk '{print "  pid "$1"  last exit "$2}'
      curl -s --max-time 3 http://127.0.0.1:8737/api/status \
        | python3 -c "
import json, sys
s = json.load(sys.stdin)
print(f\"  stream {s['stream']} | {s['req_per_min']} req/min | {s['deals_24h']} deals today\")
# a process that is up but no longer scanning looks exactly like a healthy one
# from the outside, so say it plainly
if s.get('scanner_alive') is False:
    print('  WARNING: the scanner thread has stopped — reinstall to restart it')
elif (s.get('stalled_for') or 0) > 120:
    print(f\"  WARNING: nothing scanned for {round(s['stalled_for'] / 60)} min — scanner may be stuck\")" \
        2>/dev/null || echo "  (web UI not answering yet)"
    else
      echo "not installed — run: $APPDIR/autostart.sh install"
    fi
    ;;
  log)
    tail -f "$APPDIR/scanner.log"
    ;;
  *)
    echo "usage: $0 {install|stop|uninstall|status|log}" >&2
    exit 1
    ;;
esac
