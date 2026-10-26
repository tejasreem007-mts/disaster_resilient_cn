#!/usr/bin/env bash
# Run once on an Ubuntu 20.04/22.04 VM (not WSL: it lacks the mac80211_hwsim wifi module)
set -e
sudo apt update
sudo apt install -y git tcpdump wireshark python3-pip python3-matplotlib net-tools
git clone https://github.com/intrig-unicamp/mininet-wifi ~/mininet-wifi
cd ~/mininet-wifi
sudo util/install.sh -Wlnfv
echo "Done. Test with: sudo mn --wifi"
