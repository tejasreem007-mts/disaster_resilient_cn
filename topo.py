#!/usr/bin/env python3
"""
Hybrid wired + wireless topology for Mininet-WiFi (19 nodes + controller).

Wired core : s1 s2 s3 s4 (ring + chord s1-s3)
Wireless   : ap1 ap2 ap3 (each dual-homed to two core switches)
Wired hosts: h1 (on s1), h2 (on s4)
Stations   : sta1 sta2 (ap1), sta3 (ap2), sta4 (mobile, ap2 -> ap3)

Usage:
  sudo python3 topo.py                # interactive CLI
  sudo python3 topo.py --plot         # with position graph (needs GUI)
  sudo python3 topo.py --mobility     # sta4 roams from ap2 to ap3
  sudo python3 topo.py --auto         # automated failover test + summary
"""
import argparse
import os
import re
import time

from mininet.log import setLogLevel, info
from mininet.node import RemoteController
from mn_wifi.net import Mininet_wifi
from mn_wifi.node import OVSKernelAP
from mn_wifi.cli import CLI


def dpid(n):
    return '%016x' % n


def build(plot, mobility):
    net = Mininet_wifi(controller=RemoteController, accessPoint=OVSKernelAP)
    c0 = net.addController('c0', controller=RemoteController, ip='127.0.0.1', port=6653)

    h1 = net.addHost('h1', ip='10.0.0.1/8', mac='00:00:00:00:00:01')
    h2 = net.addHost('h2', ip='10.0.0.2/8', mac='00:00:00:00:00:02')

    s1, s2, s3, s4 = [net.addSwitch('s%d' % i, dpid=dpid(i), protocols='OpenFlow13')
                      for i in range(1, 5)]

    ap1 = net.addAccessPoint('ap1', ssid='ap1', mode='g', channel='1', range=30,
                             position='20,70,0', dpid=dpid(17), protocols='OpenFlow13')
    ap2 = net.addAccessPoint('ap2', ssid='ap2', mode='g', channel='6', range=30,
                             position='50,20,0', dpid=dpid(18), protocols='OpenFlow13')
    ap3 = net.addAccessPoint('ap3', ssid='ap3', mode='g', channel='11', range=30,
                             position='80,70,0', dpid=dpid(19), protocols='OpenFlow13')

    sta1 = net.addStation('sta1', ip='10.0.0.11/8', mac='00:00:00:00:00:11', position='15,60,0')
    sta2 = net.addStation('sta2', ip='10.0.0.12/8', mac='00:00:00:00:00:12', position='25,80,0')
    sta3 = net.addStation('sta3', ip='10.0.0.13/8', mac='00:00:00:00:00:13', position='50,30,0')
    sta4 = net.addStation('sta4', ip='10.0.0.14/8', mac='00:00:00:00:00:14', position='45,35,0')

    net.setPropagationModel(model='logDistance', exp=4.5)
    cfg = getattr(net, 'configureNodes', None) or net.configureWifiNodes
    cfg()

    # wired core: ring + chord
    for a, b in [(s1, s2), (s2, s3), (s3, s4), (s4, s1), (s1, s3)]:
        net.addLink(a, b)
    # wired hosts
    net.addLink(h1, s1)
    net.addLink(h2, s4)
    # dual-homed AP uplinks (wired backhaul redundancy)
    for ap, sa, sb in [(ap1, s1, s2), (ap2, s3, s4), (ap3, s2, s3)]:
        net.addLink(ap, sa)
        net.addLink(ap, sb)

    if plot:
        net.plotGraph(max_x=100, max_y=100)
    if mobility:
        net.startMobility(time=0)
        net.mobility(sta4, 'start', time=20, position='45,35,0')
        net.mobility(sta4, 'stop', time=50, position='75,65,0')
        net.stopMobility(time=55)

    net.build()
    c0.start()
    for sw in (s1, s2, s3, s4, ap1, ap2, ap3):
        sw.start([c0])
    return net, h1, h2, sta3


def auto_test(net, h1, sta3):
    os.makedirs('results', exist_ok=True)
    info('*** Warm-up (let controller learn hosts)\n')
    h1.cmd('ping -c 3 -W 2 10.0.0.13')
    time.sleep(2)

    info('*** Starting captures (open the .pcap files in Wireshark)\n')
    h1.cmd('tcpdump -i h1-eth0 -w results/h1.pcap >/dev/null 2>&1 &')
    sta3.cmd('tcpdump -i sta3-wlan0 -w results/sta3.pcap >/dev/null 2>&1 &')
    time.sleep(1)

    info('*** h1 -> sta3 ping, 200 packets at 0.2 s (about 40 s)\n')
    h1.cmd('ping -i 0.2 -W 1 -c 200 10.0.0.13 > results/ping.txt 2>&1 &')
    time.sleep(10)
    info('*** FAILURE: s1-s4 link DOWN\n')
    net.configLinkStatus('s1', 's4', 'down')
    time.sleep(15)
    info('*** RESTORE: s1-s4 link UP\n')
    net.configLinkStatus('s1', 's4', 'up')
    time.sleep(17)
    os.system('pkill tcpdump')

    txt = open('results/ping.txt').read()
    m = re.search(r'(\d+) packets transmitted, (\d+) received', txt)
    if m:
        sent, rcv = int(m.group(1)), int(m.group(2))
        lost = sent - rcv
        out = ('sent=%d received=%d lost=%d loss=%.1f%% approx_outage=%.1fs\n'
               % (sent, rcv, lost, 100.0 * lost / sent, lost * 0.2))
    else:
        out = 'could not parse ping output, see results/ping.txt\n'
    open('results/summary.txt', 'w').write(out)
    info('*** RESULT: ' + out)


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--plot', action='store_true')
    ap.add_argument('--mobility', action='store_true')
    ap.add_argument('--auto', action='store_true')
    args = ap.parse_args()
    setLogLevel('info')
    net, h1, h2, sta3 = build(args.plot, args.mobility)
    if args.auto:
        auto_test(net, h1, sta3)
    else:
        CLI(net)
    net.stop()
