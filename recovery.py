"""
Ryu app: automated link-failure detection + alternate-path recovery
for a hybrid wired + wireless SDN (Mininet-WiFi).

Run:  ryu-manager --observe-links ryu_app/recovery.py

How it works
1. Topology discovery (LLDP) builds a weighted graph of switches and APs.
2. Hosts and stations are learned on non-inter-switch ports.
3. Unicast: Dijkstra shortest path, flows pushed along the whole path.
4. Broadcast/unknown: flooded only on host ports + a spanning tree (no loops).
5. On any link add/delete: graph is rebuilt, all flows flushed, next packet
   triggers a fresh shortest path (this is the recovery step).
6. Roaming stations: when a MAC appears on a new port, flows are flushed.
"""
import time
import networkx as nx
from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import packet, ethernet, ether_types
from ryu.topology import event
from ryu.topology.api import get_switch, get_link

FLOW_IDLE = 10   # seconds
PRIO = 10
# dpid 1 = s1, dpid 3 = s3. The chord is made more expensive so the
# primary and backup paths are predictable in demos.
LINK_COST = {frozenset((1, 3)): 2}


class Recovery(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super(Recovery, self).__init__(*args, **kwargs)
        self.dps = {}
        self.ports = {}
        self.hosts = {}        # mac -> (dpid, port)
        self.G = nx.DiGraph()
        self.link_ports = {}   # dpid -> set(port)
        self.tree_ports = {}   # dpid -> set(port)
        self.fail_t = None

    # ---------- helpers ----------
    def add_flow(self, dp, prio, match, actions, idle=0):
        ofp, p = dp.ofproto, dp.ofproto_parser
        inst = [p.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)]
        dp.send_msg(p.OFPFlowMod(datapath=dp, priority=prio, match=match,
                                 instructions=inst, idle_timeout=idle))

    def reset_flows(self, dp):
        ofp, p = dp.ofproto, dp.ofproto_parser
        dp.send_msg(p.OFPFlowMod(datapath=dp, table_id=ofp.OFPTT_ALL,
                                 command=ofp.OFPFC_DELETE,
                                 out_port=ofp.OFPP_ANY, out_group=ofp.OFPG_ANY,
                                 match=p.OFPMatch()))
        actions = [p.OFPActionOutput(ofp.OFPP_CONTROLLER, ofp.OFPCML_NO_BUFFER)]
        self.add_flow(dp, 0, p.OFPMatch(), actions)

    def rebuild(self):
        ports = {}
        for sw in get_switch(self, None):
            ports[sw.dp.id] = {x.port_no for x in sw.ports if x.port_no < 0xffffff00}
        edges = {}
        for l in get_link(self, None):
            edges[(l.src.dpid, l.dst.dpid)] = l.src.port_no
        G = nx.DiGraph()
        lp = {}
        for (s, d), port in edges.items():
            if (d, s) in edges:   # keep only bidirectional links
                w = LINK_COST.get(frozenset((s, d)), 1)
                G.add_edge(s, d, port=port, weight=w)
                lp.setdefault(s, set()).add(port)
        U = nx.Graph()
        U.add_nodes_from(G.nodes)
        for s, d, data in G.edges(data=True):
            U.add_edge(s, d, weight=data['weight'])
        tree = nx.minimum_spanning_tree(U)
        tp = {}
        for u, v in tree.edges():
            tp.setdefault(u, set()).add(G[u][v]['port'])
            tp.setdefault(v, set()).add(G[v][u]['port'])
        self.ports, self.G, self.link_ports, self.tree_ports = ports, G, lp, tp

    def flood(self, dp, in_port, msg):
        p = dp.ofproto_parser
        hostp = self.ports.get(dp.id, set()) - self.link_ports.get(dp.id, set())
        out = (hostp | self.tree_ports.get(dp.id, set())) - {in_port}
        if not out:
            return
        dp.send_msg(p.OFPPacketOut(datapath=dp, buffer_id=dp.ofproto.OFP_NO_BUFFER,
                                   in_port=in_port,
                                   actions=[p.OFPActionOutput(x) for x in out],
                                   data=msg.data))

    # ---------- events ----------
    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def features(self, ev):
        dp = ev.msg.datapath
        self.dps[dp.id] = dp
        self.reset_flows(dp)

    @set_ev_cls([event.EventSwitchEnter, event.EventSwitchLeave,
                 event.EventLinkAdd, event.EventLinkDelete])
    def topo_change(self, ev):
        if isinstance(ev, event.EventSwitchLeave):
            self.dps.pop(ev.switch.dp.id, None)
        if isinstance(ev, event.EventLinkDelete):
            if self.fail_t is None:
                self.fail_t = time.time()
            self.logger.warning("LINK DOWN detected: dpid %s <-> %s",
                                ev.link.src.dpid, ev.link.dst.dpid)
        elif isinstance(ev, event.EventLinkAdd):
            self.logger.info("LINK UP: dpid %s <-> %s",
                             ev.link.src.dpid, ev.link.dst.dpid)
        self.rebuild()
        for dp in self.dps.values():
            self.reset_flows(dp)

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in(self, ev):
        msg = ev.msg
        dp = msg.datapath
        p = dp.ofproto_parser
        in_port = msg.match['in_port']
        eth = packet.Packet(msg.data).get_protocol(ethernet.ethernet)
        if eth is None or eth.ethertype == ether_types.ETH_TYPE_LLDP:
            return
        src, dst = eth.src, eth.dst

        # learn / track host location (roaming aware)
        if in_port not in self.link_ports.get(dp.id, set()):
            loc = (dp.id, in_port)
            if self.hosts.get(src) != loc:
                moved = src in self.hosts
                self.hosts[src] = loc
                if moved:
                    self.logger.info("ROAM: %s now at dpid %s port %s", src, dp.id, in_port)
                    for d in self.dps.values():
                        self.reset_flows(d)

        if (int(dst[:2], 16) & 1) or dst not in self.hosts:
            self.flood(dp, in_port, msg)
            return

        ddp, dport = self.hosts[dst]
        if dp.id == ddp:
            path = [dp.id]
        else:
            try:
                path = nx.shortest_path(self.G, dp.id, ddp, weight='weight')
            except (nx.NetworkXNoPath, nx.NodeNotFound):
                self.logger.warning("NO PATH from dpid %s to dpid %s", dp.id, ddp)
                return

        first_out = None
        for i, node in enumerate(path):
            out = dport if i == len(path) - 1 else self.G[node][path[i + 1]]['port']
            if i == 0:
                first_out = out
            d = self.dps.get(node)
            if d:
                self.add_flow(d, PRIO, d.ofproto_parser.OFPMatch(eth_dst=dst),
                              [d.ofproto_parser.OFPActionOutput(out)], FLOW_IDLE)
        self.logger.info("PATH %s -> %s : %s", src, dst, path)
        if self.fail_t is not None:
            self.logger.warning("RECOVERY: new path installed %.1f ms after failure",
                                (time.time() - self.fail_t) * 1000)
            self.fail_t = None
        dp.send_msg(p.OFPPacketOut(datapath=dp, buffer_id=dp.ofproto.OFP_NO_BUFFER,
                                   in_port=in_port,
                                   actions=[p.OFPActionOutput(first_out)],
                                   data=msg.data))
