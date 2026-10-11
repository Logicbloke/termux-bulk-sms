#!/usr/bin/env bash
# Run bulksms as a Termux service that can be switched on and off.
#
#   ./service.sh install    once: registers the service (needs termux-services)
#   ./service.sh enable     start now and at every Termux start
#   ./service.sh disable    stop now and stay off
#   ./service.sh restart | status | logs
#
# Without termux-services, `python -m bulksms run` in a session does the same job.
set -euo pipefail

NAME=bulksms
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SVDIR="${PREFIX:-/data/data/com.termux/files/usr}/var/service"
LOGDIR="${PREFIX:-/data/data/com.termux/files/usr}/var/log/sv/$NAME"

need_sv() {
    command -v sv >/dev/null || { echo "termux-services is missing: pkg install termux-services, then restart Termux."; exit 1; }
    [ -d "$SVDIR/$NAME" ] || { echo "Not installed yet: ./service.sh install"; exit 1; }
}

case "${1:-}" in
install)
    command -v sv >/dev/null || { echo "First: pkg install termux-services, then restart Termux."; exit 1; }
    mkdir -p "$SVDIR/$NAME/log"
    cat > "$SVDIR/$NAME/run" <<RUN
#!$(command -v sh)
exec 2>&1
cd "$HERE"
exec python -u -m bulksms run
RUN
    chmod +x "$SVDIR/$NAME/run"
    ln -sf "${PREFIX:-/data/data/com.termux/files/usr}/share/termux-services/svlogger" "$SVDIR/$NAME/log/run"
    # Installed is not the same as on: it stays down until `enable`.
    touch "$SVDIR/$NAME/down"
    echo "Installed. Next: python -m bulksms set-password, then ./service.sh enable"
    ;;
enable)  need_sv; sv-enable "$NAME"; sleep 1; sv status "$NAME"; (cd "$HERE" && python -m bulksms addresses) ;;
disable) need_sv; sv-disable "$NAME"; sv down "$NAME" || true; sv status "$NAME" || true ;;
restart) need_sv; sv restart "$NAME" ;;
status)  need_sv; sv status "$NAME" ;;
logs)    tail -n 50 -f "$LOGDIR/current" ;;
*)       sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac
