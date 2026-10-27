#!/usr/bin/env python3
"""
Disaster-Resilient Communication Network (Mininet-WiFi)
Same network as public/index.html: 2 hosts, 4 core switches, 2 APs, 3 Wi-Fi stations,
plus a radio backup link. A monitor detects failed links and reroutes automatically.

  sudo python3 topology.py            interactive (CLI commands: path, cut, fix, fixall, status)
  sudo python3 topology.py --auto     scripted failure demo, prints packet loss
  options: --no-capture (skip pcap files)
"""
import heapq, http.server, json, os, subprocess, sys, threading, time
from collections import defaultdict, deque

from mininet.log import setLogLevel, info
from mininet.node import OVSKernelSwitch
from mininet.link import TCLink
from mn_wifi.net import Mininet_wifi
from mn_wifi.node import OVSKernelAP
from mn_wifi.cli import CLI

# (a, b, cost, kind)   kind: wired | radio | wifi
EDGES = [
    ('h1', 's1', 1, 'wired'),
    ('h2', 's4', 1, 'wired'),
    ('s1', 's2', 1, 'wired'),
    ('s2', 's3', 1, 'wired'),
    ('s1', 's4', 2, 'wired'),
    ('s4', 's3', 2, 'wired'),
    ('s2', 's4', 3, 'wired'),
    ('s1', 's3', 10, 'radio'),
    ('s2', 'ap1', 1, 'wired'),
    ('s3', 'ap2', 1, 'wired'),
    ('ap1', 'sta1', 1, 'wifi'),
    ('ap1', 'sta2', 1, 'wifi'),
    ('ap2', 'sta3', 1, 'wifi'),
]
HOSTS = ['h1', 'h2', 'sta1', 'sta2', 'sta3']
SWITCHES = ['s1', 's2', 's3', 's4']
STATION_AP = {'sta1': 'ap1', 'sta2': 'ap1', 'sta3': 'ap2'}
IPS = {'h1': '10.0.0.1', 'h2': '10.0.0.2',
       'sta1': '10.0.0.11', 'sta2': '10.0.0.12', 'sta3': '10.0.0.13'}

NODES, LINKS = {}, {}
LOCK = threading.Lock()
MON = None


EVENTS, EVENT_COUNT = deque(maxlen=40), [0]   # shown in the web dashboard


def log(msg):
    print('[%s] %s' % (time.strftime('%H:%M:%S'), msg), flush=True)
    EVENTS.append(msg)
    EVENT_COUNT[0] += 1


# ------------------------------------------------------------ routing
def oper_up(name):
    try:
        with open('/sys/class/net/%s/operstate' % name) as f:
            return f.read().strip() == 'up'
    except OSError:
        return False


def edge_alive(a, b):
    link = LINKS.get(frozenset((a, b)))
    if link is None:
        return True
    for intf in (link.intf1, link.intf2):
        if intf.node.name.startswith('h'):
            continue  # host side is inside its own namespace
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
                    dist[v], nxt[v] = d + c, u
                    heapq.heappush(pq, (d + c, v))
        table[dst] = nxt
    return table


def path(table, src, dst):
    if src == dst:
        return [src]
    if src not in table[dst]:
        return None
    p = [src]
    while p[-1] != dst:
        p.append(table[dst][p[-1]])
    return p


def port_of(sw, neighbor):
    link = LINKS[frozenset((sw, neighbor))]
    intf = link.intf1 if link.intf1.node.name == sw else link.intf2
    return NODES[sw].ports[intf]


def ofctl(*args):
    r = subprocess.run(['ovs-ofctl'] + list(args), stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, universal_newlines=True)
    return r.stdout + r.stderr


def install_flows():
    with LOCK:
        table = compute_routes()
        for sw in SWITCHES:
            for h in HOSTS:
                nh = table[h].get(sw)
                if nh is None:
                    ofctl('del-flows', sw, 'ip,nw_dst=%s' % IPS[h])
                else:
                    ofctl('add-flow', sw, 'priority=100,ip,nw_dst=%s,actions=output:%d'
                          % (IPS[h], port_of(sw, nh)))
        return table


class Monitor(threading.Thread):
    """Checks every link every 200 ms; on a change recomputes routes and rewrites the flows."""
    def __init__(self, interval=0.2):
        super().__init__(daemon=True)
        self.interval, self.running, self.fail_time = interval, True, None
        self.installed = frozenset()   # links the installed flows already avoid

    def snapshot(self):
        return frozenset((a, b) for a, b, _, _ in EDGES if not edge_alive(a, b))

    def run(self):
        prev = self.snapshot()
        self.installed = prev
        while self.running:
            cur = self.snapshot()
            if cur != prev:
                t0 = time.time()
                log('LINK EVENT  down links: %s' % (sorted('%s-%s' % e for e in cur) or 'none'))
                table = install_flows()
                p = path(table, 'h1', 'sta3')
                log('RECOVERED   flows rewritten in %.0f ms' % ((time.time() - t0) * 1000))
                log('NEW PATH    h1 -> sta3 : %s' % (' -> '.join(p) if p else 'UNREACHABLE'))
                if self.fail_time:
                    log('END-TO-END  failure to recovery: %.0f ms' % ((time.time() - self.fail_time) * 1000))
                    self.fail_time = None
                prev = cur
                self.installed = cur
            time.sleep(self.interval)


def set_link(a, b, state):
    link = LINKS[frozenset((a, b))]
    if state == 'down' and MON:
        MON.fail_time = time.time()
    for intf in (link.intf1, link.intf2):
        if not intf.node.name.startswith('h'):
            subprocess.run(['ip', 'link', 'set', 'dev', intf.name, state])
            break
    log('INJECT      link %s-%s %s' % (a, b, state.upper()))


# ------------------------------------------------------------ build
def build():
    # no default link class: TCLink is requested per wired link, so station-AP links stay real Wi-Fi
    net = Mininet_wifi(controller=None, accessPoint=OVSKernelAP,
                       switch=OVSKernelSwitch)
    info('*** Adding nodes\n')
    for h in ('h1', 'h2'):
        NODES[h] = net.addHost(h, ip=IPS[h] + '/24')
    pos = {'sta1': '25,55,0', 'sta2': '38,45,0', 'sta3': '95,55,0'}
    for s in ('sta1', 'sta2', 'sta3'):
        NODES[s] = net.addStation(s, ip=IPS[s] + '/24', position=pos[s])
    for s in SWITCHES:
        NODES[s] = net.addSwitch(s, cls=OVSKernelSwitch, failMode='secure')
    NODES['ap1'] = net.addAccessPoint('ap1', ssid='zoneA', mode='g', channel='1',
                                      position='30,50,0', range=60, failMode='secure')
    NODES['ap2'] = net.addAccessPoint('ap2', ssid='zoneB', mode='g', channel='6',
                                      position='90,50,0', range=60, failMode='secure')
    info('*** Configuring wifi nodes\n')
    (getattr(net, 'configureNodes', None) or net.configureWifiNodes)()

    info('*** Connecting everything\n')
    for a, b, cost, kind in EDGES:
        if kind == 'wired':
            net.addLink(NODES[a], NODES[b], cls=TCLink, bw=100, delay='2ms')
        elif kind == 'radio':
            net.addLink(NODES[a], NODES[b], cls=TCLink, bw=5, delay='30ms', loss=5)
        else:  # wifi: station joins its access point explicitly
            net.addLink(NODES[a], NODES[b])
    net.build()
    net.start()

    # addLink() may return None, so collect the link objects afterwards
    wanted = {frozenset((a, b)) for a, b, _, k in EDGES if k != 'wifi'}
    for l in net.links:
        try:
            k = frozenset((l.intf1.node.name, l.intf2.node.name))
        except AttributeError:
            continue
        if k in wanted:
            LINKS[k] = l
    for k in wanted - set(LINKS):
        a, b = sorted(k)
        found = net.linksBetween(NODES[a], NODES[b])
        if found:
            LINKS[k] = found[0]
    missing = wanted - set(LINKS)
    if missing:
        raise RuntimeError('links not found: %s' % sorted(tuple(sorted(m)) for m in missing))

    for ap in ('ap1', 'ap2'):
        ofctl('add-flow', ap, 'priority=1,actions=NORMAL')
    for a in HOSTS:                       # static ARP: no broadcast storms in the meshed core
        for b in HOSTS:
            if a != b:
                NODES[a].setARP(IPS[b], NODES[b].MAC())
    return net


# ------------------------------------------------------------ self test
def wait_for_wifi(timeout=20):
    log('WIFI        waiting for stations to associate...')
    t0 = time.time()
    pending = dict(STATION_AP)
    while pending and time.time() - t0 < timeout:
        for sta, ap in list(pending.items()):
            out = NODES[sta].cmd('iw dev %s-wlan0 link' % sta)
            if 'Connected' in out:
                log('WIFI        %s connected to %s' % (sta, ap))
                del pending[sta]
        time.sleep(1)
    for sta in pending:
        log('WIFI        %s is NOT associated' % sta)
        print(NODES[sta].cmd('iw dev'))
        print(NODES[sta].cmd('iw dev %s-wlan0 link' % sta))
    return not pending


def self_test():
    log('TEST        pinging every pair once')
    bad = []
    for a in HOSTS:
        row = []
        for b in HOSTS:
            if a == b:
                row.append(' - ')
                continue
            out = NODES[a].cmd('ping -c1 -W2 %s' % IPS[b])
            ok = ' 1 received' in out or '1 received' in out
            row.append(' ok' if ok else 'FAIL')
            if not ok:
                bad.append((a, b))
        print('   %-5s %s' % (a, ' '.join(row)))
    print('         ' + ' '.join('%-4s' % h for h in HOSTS))
    if bad:
        log('TEST        %d pair(s) failed. Flow counts per switch:' % len(bad))
        for sw in SWITCHES:
            n = max(0, len(ofctl('dump-flows', sw).strip().splitlines()) - 1)
            print('   %s has %d flow rules' % (sw, n))
    else:
        log('TEST        all hosts and stations can reach each other')
    return not bad



# ------------------------------------------------------------ live dashboard API
STATS = {'sent': 0, 'recv': 0, 'lost': 0, 'rtt': None}
HERE = os.path.dirname(os.path.abspath(__file__))


class PingStat(threading.Thread):
    """Keeps a real ping h1 -> sta3 running and counts what arrives."""
    def __init__(self, src='h1', dst='sta3'):
        super().__init__(daemon=True)
        self.proc = NODES[src].popen(['ping', '-O', '-i', '0.2', IPS[dst]],
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     universal_newlines=True)

    def run(self):
        for line in self.proc.stdout:
            if 'icmp_seq=' not in line:
                continue
            seq = int(line.split('icmp_seq=')[1].split()[0])
            STATS['sent'] = max(STATS['sent'], seq)
            if 'bytes from' in line:
                STATS['recv'] += 1
                if 'time=' in line:
                    STATS['rtt'] = float(line.split('time=')[1].split()[0])
            STATS['lost'] = max(0, STATS['sent'] - STATS['recv'] - 1)

    def stop(self):
        self.proc.terminate()


def state_json():
    return {
        'links': [{'a': a, 'b': b, 'kind': k, 'cost': c, 'up': edge_alive(a, b)}
                  for a, b, c, k in EDGES if k != 'wifi'],
        'installed_down': ['|'.join(sorted(e)) for e in (MON.installed if MON else [])],
        'stats': STATS, 'events': list(EVENTS), 'event_count': EVENT_COUNT[0],
    }


def run_demo_plan():
    t0 = time.time()
    for at, a, b, st in [(2, 's2', 's3', 'down'), (8, 's3', 's4', 'down'),
                         (14, 's2', 's3', 'up'), (19, 's3', 's4', 'up')]:
        time.sleep(max(0, at - (time.time() - t0)))
        set_link(a, b, st)


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype='application/json'):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):
        self._send(204, b'')

    def do_GET(self):
        if self.path.startswith('/api/state'):
            self._send(200, state_json())
        elif self.path in ('/', '/index.html'):
            with open(os.path.join(HERE, 'public', 'index.html'), 'rb') as f:
                self._send(200, f.read(), 'text/html; charset=utf-8')
        else:
            self._send(404, {'error': 'not found'})

    def do_POST(self):
        n = int(self.headers.get('Content-Length') or 0)
        try:
            body = json.loads(self.rfile.read(n) or b'{}')
        except ValueError:
            return self._send(400, {'error': 'bad json'})
        if self.path == '/api/link':
            a, b, st = body.get('a'), body.get('b'), body.get('state')
            if frozenset((a, b)) not in LINKS or st not in ('up', 'down'):
                return self._send(400, {'error': 'unknown link or state'})
            set_link(a, b, st)
        elif self.path == '/api/fixall':
            for k in LINKS:
                a, b = sorted(k)
                if not edge_alive(a, b):
                    set_link(a, b, 'up')
        elif self.path == '/api/demo':
            threading.Thread(target=run_demo_plan, daemon=True).start()
        else:
            return self._send(404, {'error': 'not found'})
        self._send(200, {'ok': True})


def start_api(port=8080):
    try:
        srv = http.server.ThreadingHTTPServer(('0.0.0.0', port), Handler)
    except OSError as e:
        log('API         could not start on port %d: %s' % (port, e))
        return None
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log('API         live dashboard: open http://localhost:%d in a browser on this VM' % port)
    return srv


# ------------------------------------------------------------ captures + demo
def start_captures():
    os.makedirs('captures', exist_ok=True)
    procs = []
    for l in LINKS.values():
        for intf in (l.intf1, l.intf2):
            if not intf.node.name.startswith('h'):
                procs.append(subprocess.Popen(
                    ['tcpdump', '-i', intf.name, '-U', '-w', 'captures/%s.pcap' % intf.name],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
                break
    return procs


def analyse(logfile, interval=0.2):
    seqs = set()
    for line in open(logfile):
        if 'icmp_seq=' in line:
            seqs.add(int(line.split('icmp_seq=')[1].split()[0]))
    if not seqs:
        log('RESULT      no replies recorded')
        return
    total = max(seqs)
    run = best = 0
    for i in range(1, total + 1):
        run = 0 if i in seqs else run + 1
        best = max(best, run)
    log('RESULT      sent=%d received=%d lost=%d longest outage=%.1fs'
        % (total, len(seqs), total - len(seqs), best * interval))


def auto_demo():
    os.makedirs('results', exist_ok=True)
    out = 'results/ping_h1_sta3.log'
    NODES['h1'].cmd('ping -i 0.2 -c 300 %s > %s &' % (IPS['sta3'], out))
    plan = [(8, 's2', 's3', 'down'), (22, 's3', 's4', 'down'),
            (38, 's2', 's3', 'up'), (48, 's3', 's4', 'up')]
    t0 = time.time()
    for at, a, b, st in plan:
        time.sleep(max(0, at - (time.time() - t0)))
        set_link(a, b, st)
    time.sleep(max(0, 63 - (time.time() - t0)))
    analyse(out)


class RescueCLI(CLI):
    """Same controls as the web page."""
    def do_path(self, line):
        "path [src dst]   show the route currently in use (default h1 sta3)"
        a = line.split() or ['h1', 'sta3']
        if len(a) != 2 or a[0] not in HOSTS or a[1] not in HOSTS:
            print('usage: path <src> <dst>   nodes: %s' % ' '.join(HOSTS)); return
        p = path(compute_routes(), a[0], a[1])
        print(' -> '.join(p) if p else 'no route')

    def do_cut(self, line):
        "cut <a> <b>   cut a link, e.g. cut s2 s3"
        self._flip(line, 'down')

    def do_fix(self, line):
        "fix <a> <b>   repair a link"
        self._flip(line, 'up')

    def do_fixall(self, line):
        "fixall   repair every link"
        for k, l in LINKS.items():
            a, b = sorted(k)
            if not edge_alive(a, b):
                set_link(a, b, 'up')

    def do_status(self, line):
        "status   list every link and the current h1 -> sta3 route"
        for a, b, c, kind in EDGES:
            if kind != 'wifi':
                print('  %-4s - %-4s %-6s %s' % (a, b, kind, 'up' if edge_alive(a, b) else 'DOWN'))
        self.do_path('')

    def _flip(self, line, state):
        a = line.split()
        if len(a) != 2 or frozenset(a) not in LINKS:
            print('usage: cut|fix <a> <b>   links: %s' %
                  ', '.join('%s-%s' % tuple(sorted(k)) for k in LINKS)); return
        set_link(a[0], a[1], state)


if __name__ == '__main__':
    setLogLevel('info')
    net = build()
    table = install_flows()
    log('INITIAL     h1 -> sta3 : %s' % ' -> '.join(path(table, 'h1', 'sta3') or ['none']))
    wait_for_wifi()
    self_test()
    MON = Monitor()
    MON.start()
    pinger = PingStat()
    pinger.start()
    api_srv = start_api()
    caps = [] if '--no-capture' in sys.argv else start_captures()
    try:
        if '--auto' in sys.argv:
            auto_demo()
        else:
            info('*** Commands: path | cut s2 s3 | fix s2 s3 | fixall | status | h1 ping sta3\n')
            RescueCLI(net)
    finally:
        MON.running = False
        pinger.stop()
        if api_srv:
            api_srv.shutdown()
        for p in caps:
            p.terminate()
        net.stop()
