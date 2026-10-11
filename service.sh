#!/usr/bin/env bash
# Kept for git checkouts: ./service.sh install | enable | disable | restart | status | logs
# is the same as `termux-bulk-sms service ...` (see README).
cd "$(dirname "${BASH_SOURCE[0]}")" && exec python -m termux_bulk_sms service "$@"
