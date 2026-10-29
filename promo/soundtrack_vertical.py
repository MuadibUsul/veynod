"""Synthesizes the 58s vertical promo soundtrack to promo/soundtrack_vertical.wav.

Cue times match events in promo-vertical.html. The music builds act by act
(foundation -> diagram -> loop -> federation) to carry the zoom-out.
"""
import math
from pathlib import Path

import numpy as np

from synth import (SR, add, bass, boom, buzz, clap, env, hat, kick, master, osc,
                   pad, pluck, pop_sfx, riser, siren, stamp, start, tick, whoosh, zap)

start(58.0)


def rnd(s):  # same hash as the page, so graph timings line up
    x = math.sin(s * 127.1 + 311.7) * 43758.5453
    return x - math.floor(x)


def chime(t0, m, g=0.12, pan=0.0):
    pluck(t0, m, g, pan, 0.6)
    pluck(t0 + 0.01, m + 12, g * 0.4, -pan, 0.4)


def tom(t0, m=45, g=0.5):
    n = int(0.5 * SR)
    t = np.arange(n) / SR
    f = 440.0 * 2 ** ((m - 69) / 12) * (1 + 0.6 * np.exp(-t * 18))
    add(t0, np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t * 6), g)


# ---------- ACT 1: 1 -> 10 agents (0 - 3.2) ----------
pad(0.0, [57, 60, 64], 3.3, 0.06)
pop_sfx(0.25, 72, 0.3)
boom(1.5, 0.45)
chime(1.5, 69, 0.15)
for i in range(1, 10):
    pop_sfx(1.6 + i * 0.06, 72 + (i * 2) % 12, 0.14)
chime(2.0, 76, 0.12)

# ---------- ACT 2: chaos (3.2 - 7.2), 150 bpm ----------
beat = 60 / 150
t, k = 3.2, 0
while t < 7.1:
    kick(t, 0.8)
    if k % 2:
        clap(t, 0.25)
    hat(t + beat / 2, 0.1)
    bass(t + beat / 2, [45, 46, 44, 43][(k // 2) % 4], beat / 2 * 0.9, 0.3)
    t += beat
    k += 1
for i in range(5):
    stamp(3.45 + i * 0.55, 0.45)
    pluck(3.45 + i * 0.55, 70 + i, 0.12)
    pluck(3.45 + i * 0.55, 71 + i, 0.12)
siren(5.6, 1.5, 0.12)
riser(5.0, 2.2, 0.18)

# ---------- ACT 3: brand (7.2 - 11) ----------
for i in range(10):  # "所以我们造了……" typing
    tick(7.3 + i * 0.06, 0.05, 96)
boom(8.2, 1.0)
pad(8.2, [57, 60, 64, 67], 3.0, 0.09)
for i, m in enumerate([72, 76, 79, 84, 88]):
    pluck(9.1 + i * 0.1, m, 0.1, (-1) ** i * 0.4)
pop_sfx(9.5, 79, 0.25)

# ---------- ACTS 4-7: groove under foundation / diagram / loop / federation (11 - 53) ----------
roots = [45, 41, 48, 43]  # Am F C G
chords = [[57, 60, 64], [53, 57, 60], [55, 60, 64], [55, 59, 62]]
beat, bar, t = 0.5, 0, 11.0
while t < 52.9:
    level = 0 if t < 17 else 1 if t < 29 else 2 if t < 41 else 3
    root, ch = roots[bar % 4], chords[bar % 4]
    for b in range(4):
        tb = t + b * beat
        if tb >= 52.9:
            break
        kick(tb, 0.65 + 0.07 * level)
        if b % 2 and level >= 1:
            clap(tb, 0.25)
        hat(tb + beat / 2, 0.09, 0.25)
        if level >= 2:
            hat(tb + beat / 4, 0.04, -0.3)
            hat(tb + beat * 3 / 4, 0.04, 0.3)
        if level >= 1:
            bass(tb + beat / 2, root, beat / 2 * 0.9, 0.28, "square")
    if level >= 1:
        for i in range(8):
            tb = t + i * beat / 2
            if tb < 52.9:
                pluck(tb, ch[i % 3] + 12 * (1 + (i // 3) % 2), 0.06 + 0.01 * level, (-1) ** i * 0.5)
    if bar % 2 == 0:
        pad(t, [m + 12 for m in ch] + ([ch[0] + 24] if level == 3 else []), 4.0, 0.03 + 0.012 * level)
    if level == 3 and bar % 2 == 1:
        for i in range(4):
            tom(t + 1.5 + i * 0.125, 45 - i * 3, 0.35)
    t += 4 * beat
    bar += 1
for c in [11.0, 17.0, 29.0, 41.0]:
    whoosh(c - 0.2, 0.45, 0.16)

# foundation
for i in range(6):
    pop_sfx(12.1 + i * 0.35, 72 + i * 2, 0.18)
whoosh(15.9, 0.5, 0.15)
stamp(16.2, 0.45)

# diagram
for i in range(9):
    tick(18.2 + i * 0.1, 0.05, 96)
for c in range(5):
    pop_sfx(19.7 + c * 0.3, 67 + c * 3, 0.14)
pop_sfx(21.55, 72, 0.3)  # confirm
for i, m in enumerate([96, 100, 103, 108]):  # freeze shimmer
    pluck(22.0 + i * 0.05, m, 0.07, (-1) ** i * 0.5, 0.8)
cols = [3, 5, 6, 5, 3]
nodes = {}
for c, n in enumerate(cols):
    for j in range(n):
        parents = [] if c == 0 else [j * cols[c - 1] // n]
        if c and rnd(c * 10 + j) > 0.45:
            parents.append((parents[0] + 1) % cols[c - 1])
        nodes[(c, j)] = sorted(set(parents))
times = {}
for (c, j), ps in nodes.items():
    d = max(times[(c - 1, p)][1] for p in ps) + 0.15 if ps else 22.7 + j * 0.06
    m = d + (1.7 if (c, j) == (2, 2) else 0.5 + rnd(c * 7 + j) * 0.3)
    times[(c, j)] = (d, m)
scale = [69, 72, 74, 76, 79, 81, 84, 86, 88]
for (c, j), (d, m) in times.items():
    chime(m, scale[(c * 2 + j) % len(scale)], 0.07, (j % 3 - 1) * 0.5)
zap(times[(2, 2)][0] + 0.35, 0.28)
done = max(m for _, m in times.values())
for i, m in enumerate([72, 76, 79, 84]):
    chime(done + i * 0.08, m, 0.12)

# loop
pop_sfx(30.4, 70, 0.25)
pop_sfx(30.9, 77, 0.3)
for i in range(6):
    pop_sfx(31.0 + i * 0.08, 74 + i * 2, 0.12)
for i in range(4):
    chime(32.2 + i * 0.45, [76, 79, 83, 86][i], 0.1, (-1) ** i * 0.5)
for i in [0, 1, 3]:
    tick(36.0 + i * 0.45 + 0.385, 0.18, 80)
    pop_sfx(36.0 + i * 0.45 + 0.55, 84, 0.12)
whoosh(36.9 + 0.3, 0.4, 0.15)
chime(37.85, 88, 0.15)
chime(38.05, 84, 0.15)  # notification ding-dong
for b in [39.26, 39.63]:
    add(b, osc(1000.0, 0.1, "sine") * env(int(0.1 * SR), 0.002, 0.05), 0.15)
add(40.0, osc(1000.0, 0.6, "sine") * env(int(0.6 * SR), 0.005, 9, 1, 0.05), 0.1)
buzz(40.25, 0.25)

# federation
whoosh(41.0, 0.7, 0.2)
pop_sfx(41.75, 67, 0.3)
for i in range(6):
    chime(42.4 + i * 0.22, [69, 72, 76, 79, 81, 84][i], 0.11, (i % 3 - 1) * 0.6)
stamp(45.0, 0.4)
chime(45.05, 81, 0.12)
whoosh(47.6, 0.6, 0.18)
pop_sfx(48.2, 76, 0.2)
whoosh(49.5, 0.6, 0.18)
pop_sfx(50.0, 72, 0.25)
riser(49.0, 1.6, 0.2)
boom(50.6, 0.9)
pad(50.6, [45, 57, 64, 69, 72, 76], 2.4, 0.08)
for i in range(24):
    pluck(50.6 + rnd(i + 31) * 1.4, [84, 88, 91, 96][i % 4], 0.04, (rnd(i) - 0.5) * 1.6, 0.3)
pop_sfx(51.2, 69, 0.3)

# ---------- ACT 8: call to action (53 - 58) ----------
for i in range(5):
    pop_sfx(53.1 + i * 0.32, 69 + i * 3, 0.2)
    pluck(53.1 + i * 0.32, [69, 72, 76, 79, 84][i], 0.12)
boom(55.0, 0.6)
pad(55.0, [57, 60, 64, 69, 72], 3.0, 0.09)
for m in [69, 76, 81]:
    pluck(55.0, m, 0.15, 0.0, 0.8)
pop_sfx(55.6, 81, 0.2)

master(Path(__file__).with_name("soundtrack_vertical.wav"))
