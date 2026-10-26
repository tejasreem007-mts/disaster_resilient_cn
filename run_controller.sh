#!/usr/bin/env bash
source "$HOME/ryu-env/bin/activate"
cd "$(dirname "$0")/.."
ryu-manager --observe-links ryu_app/recovery.py
