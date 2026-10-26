#!/usr/bin/env python3
"""Generates docs/topology.png (wired vs wireless vs radio backup)."""
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch
from matplotlib.lines import Line2D

pos = {'h1': (0, 3), 's1': (2, 3), 's2': (5, 5), 's4': (5, 1), 's3': (8, 3),
       'h2': (5, -0.8), 'ap1': (5, 7.2), 'ap2': (11, 3),
       'sta1': (3.4, 8.6), 'sta2': (6.8, 8.6), 'sta3': (13, 3)}
label = {'h1': 'h1\nCommand Centre\n(sender)', 'h2': 'h2\nHospital', 'sta1': 'sta1\nRescue A',
         'sta2': 'sta2\nRescue B', 'sta3': 'sta3\nField Team\n(receiver)'}
wired = [('h1','s1'),('h2','s4'),('s1','s2'),('s2','s3'),('s1','s4'),('s4','s3'),('s2','s4'),
         ('s2','ap1'),('s3','ap2')]
radio = [('s1','s3')]
wifi = [('ap1','sta1'),('ap1','sta2'),('ap2','sta3')]

fig, ax = plt.subplots(figsize=(13, 8.5))
for a, b in wired:
    ax.plot(*zip(pos[a], pos[b]), color='#1f4e79', lw=2.6, zorder=1)
for a, b in radio:
    (x1, y1), (x2, y2) = pos[a], pos[b]
    ax.annotate('', xy=(x2, y2), xytext=(x1, y1),
                arrowprops=dict(arrowstyle='<->', color='#c0392b', lw=2.4, ls='--',
                                connectionstyle='arc3,rad=0.6'), zorder=1)
for a, b in wifi:
    ax.plot(*zip(pos[a], pos[b]), color='#27ae60', lw=2.2, ls=':', zorder=1)
cost = {('s1','s2'):'1',('s2','s3'):'1',('s1','s4'):'2',('s4','s3'):'2',('s2','s4'):'3'}
for (a, b), c in cost.items():
    ax.text((pos[a][0]+pos[b][0])/2+.15, (pos[a][1]+pos[b][1])/2+.15, 'cost '+c, fontsize=8, color='#1f4e79')
ax.text(9.6, 0.9, 'radio backup s1-s3\n5 Mbps, 30 ms, 5% loss, cost 10', color='#c0392b', fontsize=9, ha='center', va='center')

def box(n, color, shape='round'):
    x, y = pos[n]
    ax.text(x, y, label.get(n, n), ha='center', va='center', fontsize=9, fontweight='bold',
            zorder=3, bbox=dict(boxstyle='round,pad=0.45' if shape == 'round' else 'square,pad=0.45',
                                fc=color, ec='black', lw=1.2))
for n in ('s1','s2','s3','s4'): box(n, '#aed6f1', 'sq')
for n in ('h1','h2'): box(n, '#f9e79f')
for n in ('ap1','ap2'): box(n, '#abebc6', 'sq')
for n in ('sta1','sta2','sta3'): box(n, '#d5f5e3')
ax.text(5, 9.8, 'Disaster-Resilient Communication Network (hybrid wired + wireless)',
        ha='center', fontsize=14, fontweight='bold')
ax.legend(handles=[Line2D([0],[0],color='#1f4e79',lw=2.6,label='Wired (100 Mbps, 2 ms)'),
                   Line2D([0],[0],color='#c0392b',lw=2.4,ls='--',label='Radio backhaul (backup)'),
                   Line2D([0],[0],color='#27ae60',lw=2.2,ls=':',label='Wi-Fi (AP to station)')],
          loc='lower right', fontsize=10)
ax.set_xlim(-1.5, 14.5); ax.set_ylim(-2, 10.3); ax.axis('off')
plt.tight_layout(); plt.savefig('docs/topology.png', dpi=160)
print('saved docs/topology.png')
