#!/usr/bin/env bash
cd "$(dirname "$0")/.."
sudo mn -c >/dev/null 2>&1
sudo python3 topo.py "$@"
