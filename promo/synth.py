"""Tiny numpy synth shared by the promo soundtracks: instruments, SFX, mixdown.

Call start(duration) first, place sounds with the helpers, then master(path).
"""
import wave

import numpy as np

SR = 44100
N = 0
out = np.zeros((0, 2))
rng = np.random.default_rng(7)


def start(duration):
    """Reset the mix buffer to `duration` seconds of silence."""
    global N, out, rng
    N = int(SR * duration)
    out = np.zeros((N, 2))
    rng = np.random.default_rng(7)


def note(n):  # MIDI note -> Hz
    return 440.0 * 2 ** ((n - 69) / 12)


def env(n, a=0.005, d=0.2, s=0.0, r=0.05, hold=None):
    """Simple ADSR envelope of n samples."""
    t = np.arange(n) / SR
    hold = n / SR - r if hold is None else hold
    e = np.where(t < a, t / max(a, 1e-6), s + (1 - s) * np.exp(-(t - a) / max(d, 1e-6)))
    rel = np.clip((t - hold) / max(r, 1e-6), 0, 1)
    return e * (1 - rel)


def add(t0, sig, gain=1.0, pan=0.0):
    i = int(t0 * SR)
    if i >= N:
        return
    sig = sig[: N - i]
    out[i : i + len(sig), 0] += sig * gain * (1 - max(0, pan))
    out[i : i + len(sig), 1] += sig * gain * (1 + min(0, pan))


def osc(freq, dur, kind="sine", detune=0.0):
    t = np.arange(int(dur * SR)) / SR
    f = np.broadcast_to(freq, t.shape) if np.ndim(freq) else freq
    ph = 2 * np.pi * np.cumsum(f * (1 + detune) * np.ones_like(t)) / SR
    if kind == "sine":
        return np.sin(ph)
    if kind == "saw":
        return 2 * ((ph / (2 * np.pi)) % 1) - 1
    if kind == "square":
        return np.sign(np.sin(ph))
    if kind == "tri":
        return 2 * np.abs(2 * ((ph / (2 * np.pi)) % 1) - 1) - 1
    raise ValueError(kind)


def lowpass(x, alpha):
    y = np.empty_like(x)
    acc = 0.0
    for i, v in enumerate(x):
        acc += alpha * (v - acc)
        y[i] = acc
    return y


# ---------- instruments ----------
def kick(t0, g=0.9):
    n = int(0.35 * SR)
    t = np.arange(n) / SR
    f = 50 + 110 * np.exp(-t * 30)
    add(t0, np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t * 9), g)


def clap(t0, g=0.35):
    n = int(0.2 * SR)
    noise = rng.standard_normal(n)
    noise = noise - lowpass(noise, 0.08)
    add(t0, noise * env(n, 0.001, 0.06), g)


def hat(t0, g=0.12, pan=0.2):
    n = int(0.06 * SR)
    noise = rng.standard_normal(n)
    noise = noise - lowpass(noise, 0.5)
    add(t0, noise * env(n, 0.001, 0.015), g, pan)


def bass(t0, m, dur, g=0.35, kind="saw"):
    s = osc(note(m), dur, kind) * 0.6 + osc(note(m - 12), dur, "sine") * 0.6
    add(t0, lowpass(s, 0.08) * env(len(s), 0.005, 0.25, 0.5, 0.05), g)


def pluck(t0, m, g=0.18, pan=0.0, dur=0.35):
    s = osc(note(m), dur, "tri") + 0.4 * osc(note(m + 12), dur, "sine")
    add(t0, s * env(len(s), 0.002, 0.12), g, pan)


def pad(t0, notes, dur, g=0.08):
    s = sum(osc(note(m), dur, "saw", d) for m in notes for d in (-0.003, 0.003))
    s = lowpass(s, 0.03)
    add(t0, s * env(len(s), 0.6, 10, 1.0, 0.8), g)


def pop_sfx(t0, m=84, g=0.3):
    n = int(0.15 * SR)
    t = np.arange(n) / SR
    f = note(m) * (1 + 1.5 * np.exp(-t * 40))
    add(t0, np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t * 30), g)


def whoosh(t0, dur=0.45, g=0.18):
    n = int(dur * SR)
    noise = rng.standard_normal(n)
    sweep = np.linspace(0.02, 0.4, n)
    y, acc = np.empty(n), 0.0
    for i in range(n):
        acc += sweep[i] * (noise[i] - acc)
        y[i] = acc
    add(t0, y * np.sin(np.linspace(0, np.pi, n)) ** 2, g)


def tick(t0, g=0.25, m=88):
    n = int(0.05 * SR)
    add(t0, osc(note(m), 0.05, "square") * env(n, 0.001, 0.012), g)


def boom(t0, g=0.9):
    n = int(2.2 * SR)
    t = np.arange(n) / SR
    f = 30 + 70 * np.exp(-t * 6)
    s = np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t * 1.6)
    noise = lowpass(rng.standard_normal(n), 0.05) * np.exp(-t * 4) * 0.8
    add(t0, s + noise, g)


def zap(t0, g=0.25):
    n = int(0.35 * SR)
    t = np.arange(n) / SR
    f = 1800 * np.exp(-t * 8) + 120
    add(t0, np.sign(np.sin(2 * np.pi * np.cumsum(f) / SR)) * np.exp(-t * 7), g)


def buzz(t0, g=0.25):
    s = osc(note(40), 0.45, "square") + osc(note(41), 0.45, "square")
    add(t0, lowpass(s, 0.15) * env(len(s), 0.005, 1, 1, 0.05), g)


def siren(t0, dur, g=0.12):
    t = np.arange(int(dur * SR)) / SR
    f = 700 + 300 * np.sin(2 * np.pi * 3 * t)
    add(t0, np.sin(2 * np.pi * np.cumsum(f) / SR) * env(len(t), 0.02, 9, 1, 0.1), g)


def riser(t0, dur, g=0.15):
    n = int(dur * SR)
    t = np.arange(n) / SR
    f = 200 * 2 ** (t / dur * 3)
    s = np.sin(2 * np.pi * np.cumsum(f) / SR) * 0.5 + rng.standard_normal(n) * 0.3
    add(t0, s * (t / dur) ** 2, g)


def stamp(t0, g=0.6):
    kick(t0, g)
    clap(t0, g * 0.6)


def master(path, fade_s=0.6):
    """Fade out, soft-clip, normalize and write a 16-bit stereo WAV."""
    global out
    fade = np.ones(N)
    fade[-int(fade_s * SR) :] = np.linspace(1, 0, int(fade_s * SR))
    out *= fade[:, None]
    out = np.tanh(out * 1.1)
    out /= np.max(np.abs(out)) / 0.89
    pcm = (out * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())
    print("wrote", path)
