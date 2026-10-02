#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# One-time setup for macOS/Linux (double-clickable in Finder).
# Registers the "terminal" launcher with your browser so the terminal icon in the
# Multi-Script chat bar can start/stop the bridge itself. Safe to re-run.
cd "$(dirname "$0")" || exit 1

if [ ! -f runtime/install_native_host.py ]; then
    echo "ERROR: runtime/install_native_host.py not found. Extract the WHOLE download, then run this again."
    read -n 1 -s -r -p "Press any key to close..."; echo
    exit 1
fi

PY=""
for c in python3 python; do
    if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info>=(3,9) else 1)' >/dev/null 2>&1; then PY="$c"; break; fi
done
if [ -z "$PY" ]; then
    echo "Python 3.9 or newer is required. Install it from https://www.python.org/downloads/ and run this again."
    read -n 1 -s -r -p "Press any key to close..."; echo
    exit 1
fi

chmod +x runtime/native_host.py 2>/dev/null
"$PY" runtime/install_native_host.py "$@"
rc=$?
echo
read -n 1 -s -r -p "Press any key to close this window..."; echo
exit $rc
