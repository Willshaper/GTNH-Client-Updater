#!/usr/bin/env bash
# Linux/macOS launcher. Note: this updater is built for Windows + Prism Launcher;
# on other systems set FIXED_INSTANCE_DIR in update_gtnh.py to your instance path.
cd "$(dirname "$0")" || exit 1

if command -v python3 >/dev/null 2>&1; then
    python3 update_gtnh.py
elif command -v python >/dev/null 2>&1; then
    python update_gtnh.py
else
    echo "Python 3 was not found. Install it (e.g. 'sudo apt install python3') and try again."
    read -r -p "Press Enter to exit..."
fi
