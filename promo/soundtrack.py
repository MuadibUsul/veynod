"""Synthesizes the 50s promo soundtrack (music + SFX) to promo/soundtrack.wav.

Every cue time below matches an event in promo.html, so the audio lands on the
visuals without any manual editing. Pure numpy; no samples or downloads.
"""
from pathlib import Path

from synth import (bass, boom, buzz, clap, hat, kick, master, pad, pluck, pop_sfx,
                   riser, siren, stamp, start, tick, whoosh, zap)

start(50.0)

# ---------- ACT 1: intro (0 - 4.5) ----------
pad(0.0, [60, 64, 67, 71], 4.6, 0.07)  # Cmaj7
for i, c in enumerate([1.2, 1.4, 1.6, 1.8]):
    pop_sfx(c, 76 + i * 4)
for i, m in enumerate([72, 76, 79, 83, 84]):
    pluck(2.8 + i * 0.18, m, 0.12, (-1) ** i * 0.3)

# ---------- ACT 2: chaos (4.5 - 15.5), 140 bpm, minor & detuned ----------
beat = 60 / 140
t = 4.5
k = 0
while t < 15.3:
    kick(t, 0.8)
    if k % 2:
        clap(t, 0.25)
    hat(t + beat / 2, 0.1)
    root = [45, 45, 46, 44][(k // 4) % 4]
    bass(t + beat / 2, root, beat / 2 * 0.9, 0.3)
    t += beat
    k += 1
whoosh(5.35)
for i in range(5):  # failure label stings
    pluck(5.5 + i * 2, 69 + i, 0.15)
    pluck(5.5 + i * 2, 70 + i, 0.15)
riser(5.5, 1.6, 0.1)  # context inflates
boom(7.1, 0.5)  # 💥
stamp(8.8, 0.7)  # DEADLOCK stamp
v = 0.0
while v < 2.0:  # ping-pong hits every 0.25s
    tick(9.5 + v, 0.22, 88 if int(v / 0.25) % 2 else 84)
    v += 0.25
whoosh(12.2, 0.4, 0.2)  # note slapped on
pop_sfx(12.55, 60, 0.4)  # 😱
for i in range(18):  # terminal typing
    tick(13.5 + i * 0.05, 0.06, 96)
siren(14.6, 0.9, 0.14)
riser(13.6, 1.9, 0.18)

# ---------- ACT 3: brand reveal (15.5 - 20) ----------
boom(16.4, 1.0)
pad(16.4, [57, 60, 64, 67], 3.8, 0.09)
for i, m in enumerate([72, 76, 79, 84, 88, 91]):
    pluck(17.4 + i * 0.12, m, 0.1, (-1) ** i * 0.4)
pop_sfx(18.5, 79, 0.25)

# ---------- ACT 4: features (20 - 45.5), 120 bpm groove ----------
beat = 0.5
prog_roots = [48, 45, 41, 43]  # C  Am  F  G (one per bar)
chords = [[60, 64, 67], [57, 60, 64], [53, 57, 60], [55, 59, 62]]
bar = 0
t = 20.0
while t < 45.4:
    root = prog_roots[bar % 4]
    for b in range(4):
        tb = t + b * beat
        if tb >= 45.4:
            break
        kick(tb, 0.75)
        if b % 2:
            clap(tb, 0.25)
        hat(tb + beat / 2, 0.1, 0.25)
        hat(tb + beat / 4 * 3, 0.05, -0.25)
        bass(tb + beat / 2, root, beat / 2 * 0.9, 0.3, "square")
    arp = chords[bar % 4]
    for i in range(8):
        tb = t + i * beat / 2
        if tb < 45.4:
            pluck(tb, arp[i % 3] + 12 * (i // 3 % 2), 0.08, (-1) ** i * 0.5)
    if bar % 2 == 0:
        pad(t, [m + 12 for m in arp], 4.0, 0.035)
    t += 4 * beat
    bar += 1
for c in [20, 23.5, 27, 30.5, 34, 37.5, 41]:
    whoosh(c - 0.15, 0.4, 0.14)
for i in range(3):  # F1 cards landing + stamp
    pop_sfx(20.6 + i * 0.35, 72 + i * 3, 0.18)
stamp(21.75, 0.5)
pop_sfx(22.5, 65, 0.15)
pop_sfx(25.5, 90, 0.2)  # heartbeat renews lease
pop_sfx(25.65, 90, 0.15)
pluck(26.4, 84, 0.15)
pluck(26.5, 88, 0.15)  # delivered
for i in range(4):  # F3 pings
    tick(27.3 + i * 0.4, 0.2, 84 if i % 2 else 88)
zap(28.9, 0.3)
pop_sfx(29.4, 72, 0.25)
buzz(31.6, 0.3)  # 403 bounce
for i in range(5):
    pop_sfx(34.3 + i * 0.1, 76 + i * 2, 0.12)
for i in range(4):
    pop_sfx(37.7 + i * 0.15, 79 + i * 3, 0.15)
pop_sfx(39.7, 60, 0.35)  # undo press
for i, m in enumerate([84, 79, 76, 72]):  # rewind arpeggio
    pluck(39.75 + i * 0.07, m, 0.12)
for i in range(3):
    whoosh(41.3 + i * 0.25, 0.3, 0.08)

# ---------- ACT 5: finale (45.5 - 50) ----------
kick(45.5, 0.9)
pad(45.5, [60, 64, 67, 72, 76], 4.4, 0.08)
for i in range(16):
    t = 45.5 + i * 0.25
    pluck(t, [72, 76, 79, 84][i % 4] + (12 if i >= 8 else 0), 0.09, (-1) ** i * 0.5)
    if i % 2 == 0:
        hat(t, 0.08)
    if i % 4 == 0:
        kick(t, 0.6)
for i in range(18):
    tick(46.2 + i * 0.05, 0.05, 96)
pluck(47.4, 84, 0.18)
pluck(47.4, 88, 0.18)
boom(48.3, 0.45)
pluck(48.3, 72, 0.2)
pluck(48.3, 79, 0.2)
pluck(48.3, 84, 0.2)

master(Path(__file__).with_name("soundtrack.wav"))
