#!/usr/bin/env python3
"""
Disaster-Resilient Communication Network
Automated link-failure detection + alternate-path recovery (Mininet-WiFi)

Run:  sudo python3 topology.py            (interactive CLI)
      sudo python3 topology.py --auto     (scripted failure demo, then exit)
"""
import heapq, os, subprocess, sys, threading, time
from collections import defaultdict

from mininet.log import setLogLevel, info
from mininet.node import OVSKernelSwitch
from mininet.link import TCLink
from mn_wifi.net import Mininet_wifi
from mn_wifi.node import OVSKernelAP
from mn_wifi.cli import CLI

# ---------------------------------------------------------------- topology
# (a, b, cost, kind)   kind: wired | radio (backup microwave link) | wifi
EDGES = [
    ('h1', 's1', 1, 'wired'),   # command centre (sender)
    ('h2', 's4', 1, 'wired'),   # hospital server
    ('s1', 's2', 1, 'wired'),
    ('s2', 's3', 1, 'wired'),   # primary path to s3
    ('s1', 's4', 2, 'wired'),   # first alternate
    ('s4', 's3', 2, 'wired'),
    ('s2', 's4', 3, 'wired'),   # cross link
    ('s1', 's3', 10, 'radio'),  # last-resort radio backhaul (lossy, slow)
    ('s2', 'ap1', 1, 'wired'),
    ('s3', 'ap2', 1, 'wired'),
    ('ap1', 'sta1', 1, 'wifi'),
    ('ap1', 'sta2', 1, 'wifi'),
    ('ap2', 'sta3', 1, 'wifi'),  # field receiver (rescue team)
]
HOSTS = ['h1', 'h2', 'sta1', 'sta2', 'sta3']
SWITCHES = ['s1', 's2', 's3', 's4']
IPS = {'h1': '10.0.0.1', 'h2': '10.0.0.2',
       'sta1': '10.0.0.11', 'sta2': '10.0.0.12', 'sta3': '10.0.0.13'}

NODES = {}   # name -> node object
LINKS = {}   # frozenset({a,b}) -> Link (wired/radio only)
LOCK = threading.Lock()


def log(msg):
    print('[%s] %s' % (time.strftime('%H:%M:%S'), msg), flush=True)


# ---------------------------------------------------------------- routing
def oper_up(intf_name):
    try:
        with open('/sys/class/net/%s/operstate' % intf_name) as f:
            return f.read().strip() == 'up'
    except OSError:
        return False


def edge_alive(a, b):
    link = LINKS.get(frozenset((a, b)))
    if link is None:
        return True  # wifi association: not failed in this demo
    for intf in (link.intf1, link.intf2):
        if intf.node.name.startswith('h'):
            continue  # host side lives in its own namespace
        if not oper_up(intf.name):
            return False
    return True


def compute_routes():
    adj = defaultdict(list)
    for a, b, c, _ in EDGES:
        if edge_alive(a, b):
            adj[a].append((b, c))
            adj[b].append((a, c))
    table = {}
    for dst in HOSTS:
        dist, nxt, pq = {dst: 0}, {}, [(0, dst)]
        while pq:
            d, u = heapq.heappop(pq)
            if d > dist[u]:
                continue
            for v, c in adj[u]:
                if d + c < dist.get(v, 1e9):
                    dist[v] = d + c
                    nxt[v] = u
                    heapq.heappush(pq, (d + c, v))
        table[dst] = nxt
    return table


def path(table, src, dst):
    if src != dst and src not in table[dst]:
        return None
    p = [src]
    while p[-1] != dst:
        p.append(table[dst][p[-1]])
    return p


def port_of(sw, neighbor):
    link = LINKS[frozenset((sw, neighbor))]
    intf = link.intf1 if link.intf1.node.name == sw else link.intf2
    return NODES[sw].ports[intf.name]


def ofctl(*args):
    try:
        subprocess.run(['ovs-ofctl'] + list(args), check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        log('OFCTL ERROR: %s' % e)


def install_flows():
    with LOCK:
        table = compute_routes()
        for sw in SWITCHES:
            for h in HOSTS:
                match = 'priority=100,ip,nw_dst=%s' % IPS[h]
                nh = table[h].get(sw)
                if nh is None:
                    ofctl('del-flows', sw, 'ip,nw_dst=%s' % IPS[h])
                else:
                    try:
                        port = port_of(sw, nh)
                        ofctl('add-flow', sw, '%s,actions=output:%d' % (match, port))
                    except KeyError:
                        # Skip wifi links (not in LINKS) - AP learns via NORMAL
                        pass
        return table


class Monitor(threading.Thread):
    """Polls link state every 200 ms; on any change recomputes + reinstalls routes."""
    def __init__(self, interval=0.2):
        super().__init__(daemon=True)
        self.interval, self.running = interval, True
        self.fail_time = None   # set by demo to measure detect+recover time

    def snapshot(self):
        return frozenset((a, b) for a, b, _, _ in EDGES if not edge_alive(a, b))

    def run(self):
        prev = self.snapshot()
        while self.running:
            cur = self.snapshot()
            if cur != prev:
                t0 = time.time()
                down = sorted('%s-%s' % e for e in cur)
                log('LINK EVENT  down links: %s' % (down or 'none'))
                table = install_flows()
                dt = (time.time() - t0) * 1000
                p = path(table, 'h1', 'sta3')
                log('RECOVERED   flows reinstalled in %.0f ms' % dt)
                log('NEW PATH    h1 -> sta3 : %s' % (' -> '.join(p) if p else 'UNREACHABLE'))
                if self.fail_time:
                    log('END-TO-END  failure to recovery: %.0f ms' % ((time.time() - self.fail_time) * 1000))
                    self.fail_time = None
                prev = cur
            time.sleep(self.interval)


def set_link(a, b, state, mon=None):
    link = LINKS[frozenset((a, b))]
    if state == 'down' and mon:
        mon.fail_time = time.time()
    for intf in (link.intf1, link.intf2):
        if not intf.node.name.startswith('h'):
            subprocess.run(['ip', 'link', 'set', 'dev', intf.name, state], check=False)
            break
    log('INJECT      link %s-%s %s' % (a, b, state.upper()))


# ---------------------------------------------------------------- build
def build():
    net = Mininet_wifi(controller=None, accessPoint=OVSKernelAP,
                       switch=OVSKernelSwitch, link=TCLink)
    info('*** Adding nodes\n')
    for h in ('h1', 'h2'):
        NODES[h] = net.addHost(h, ip=IPS[h] + '/24')
    pos = {'sta1': '22,58,0', 'sta2': '36,44,0', 'sta3': '96,56,0'}
    for s in ('sta1', 'sta2', 'sta3'):
        NODES[s] = net.addStation(s, ip=IPS[s] + '/24', position=pos[s])
    for s in SWITCHES:
        NODES[s] = net.addSwitch(s, cls=OVSKernelSwitch, failMode='secure')
    NODES['ap1'] = net.addAccessPoint('ap1', ssid='zoneA', mode='g', channel='1',
                                      position='30,50,0', failMode='secure')
    NODES['ap2'] = net.addAccessPoint('ap2', ssid='zoneB', mode='g', channel='6',
                                      position='90,50,0', failMode='secure')
    net.setPropagationModel(model='logDistance', exp=4.5)
    info('*** Configuring wifi nodes\n')
    net.configureWifiNodes()

    info('*** Adding wired + radio links\n')
    for a, b, cost, kind in EDGES:
        if kind == 'wifi':
            continue
        if kind == 'radio':
            l = net.addLink(NODES[a], NODES[b], cls=TCLink, bw=5, delay='30ms', loss=5)
        else:
            l = net.addLink(NODES[a], NODES[b], cls=TCLink, bw=100, delay='2ms')
        LINKS[frozenset((a, b))] = l
    net.build()
    net.start()
    for ap in ('ap1', 'ap2'):
        ofctl('add-flow', ap, 'table=0,priority=1,actions=NORMAL')  # APs have one uplink: safe to learn
    # static ARP so no broadcast storms in the meshed core
    for a in HOSTS:
        for b in HOSTS:
            if a != b:
                NODES[a].setARP(IPS[b], NODES[b].MAC())
    return net


def start_captures():
    os.makedirs('captures', exist_ok=True)
    procs = []
    for key, l in LINKS.items():
        for intf in (l.intf1, l.intf2):
            if intf.node.name.startswith('h'):
                continue
            procs.append(subprocess.Popen(
                ['tcpdump', '-i', intf.name, '-U', '-w', 'captures/%s.pcap' % intf.name],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
            break   # one side of each link is enough
    return procs


def analyse(logfile, interval=0.2):
    seqs, total = set(), 0
    for line in open(logfile):
        if 'icmp_seq=' in line:
            seqs.add(int(line.split('icmp_seq=')[1].split()[0]))
    if not seqs:
        return
    total = max(seqs)
    miss = [i for i in range(1, total + 1) if i not in seqs]
    run = best = 0
    for i in range(1, total + 1):
        run = run + 1 if i not in seqs else 0
        best = max(best, run)
    log('RESULT      sent=%d received=%d lost=%d longest outage=%.1fs'
        % (total, len(seqs), len(miss), best * interval))


def auto_demo(net, mon):
    os.makedirs('results', exist_ok=True)
    out = 'results/ping_h1_sta3.log'
    NODES['h1'].cmd('ping -i 0.2 -c 300 %s > %s &' % (IPS['sta3'], out))
    plan = [(8, 's2', 's3', 'down'), (22, 's3', 's4', 'down'),
            (38, 's2', 's3', 'up'), (48, 's3', 's4', 'up')]
    t0 = time.time()
    for at, a, b, st in plan:
        time.sleep(max(0, at - (time.time() - t0)))
        set_link(a, b, st, mon)
    time.sleep(max(0, 62 - (time.time() - t0)))
    analyse(out)


if __name__ == '__main__':
    setLogLevel('info')
    net = build()
    table = install_flows()
    log('INITIAL     h1 -> sta3 : %s' % ' -> '.join(path(table, 'h1', 'sta3')))
    mon = Monitor(); mon.start()
    caps = [] if '--no-capture' in sys.argv or True else start_captures()  # tcpdump often unavailable
    try:
        if '--auto' in sys.argv:
            auto_demo(net, mon)
        else:
            info('*** Try: h1 ping sta3 | link s2 s3 down | link s2 s3 up | links\n')
            CLI(net)
    finally:
        mon.running = False
        for p in caps:
            p.terminate()
        try:
            net.stop()
        except Exception as e:
            log('NET STOP ERROR: %s' % e)
