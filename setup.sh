#!/usr/bin/env bash
# Ubuntu 20.04 / 22.04 VM. Run once from the repo root.
set -e
sudo apt update
sudo apt install -y git python3-pip python3-venv mininet openvswitch-switch tcpdump wireshark net-tools iperf
sudo service openvswitch-switch start
if [ ! -d "$HOME/mininet-wifi" ]; then
  git clone https://github.com/intrig-unibo/mininet-wifi "$HOME/mininet-wifi"
  (cd "$HOME/mininet-wifi" && sudo util/install.sh -Wlnfv)
fi
python3 -m venv "$HOME/ryu-env"
source "$HOME/ryu-env/bin/activate"
pip install --upgrade pip "setuptools<68" wheel
pip install -r requirements.txt
echo "Setup done."
