"""Contrast gate for the Veynod console's core semantic pairs."""

from __future__ import annotations

PAIRS = [
    # Console, night shift (dashboard/app/design-tokens.css).
    ("text-primary/page", "#eceee9", "#131209", 4.5),
    ("text-secondary/page", "#a9b3ac", "#131209", 4.5),
    ("text-muted/page", "#8e9890", "#131209", 4.5),
    ("text-muted/panel", "#8e9890", "#181818", 4.5),
    ("text-secondary/panel", "#a9b3ac", "#181818", 4.5),
    ("primary-button", "#14201a", "#f5b21b", 4.5),
    ("accent/page", "#f7c65a", "#131209", 4.5),
    ("success/page", "#4cc27e", "#131209", 4.5),
    ("danger/page", "#f06a5a", "#131209", 4.5),
    ("warning/page", "#f5b21b", "#131209", 4.5),
    ("info/page", "#86a9f0", "#131209", 4.5),
    ("rail-text", "#e7ebe5", "#10362a", 4.5),
    ("rail-text-secondary", "#a9c2b5", "#10362a", 4.5),
    ("control-border/panel", "#6b6b6b", "#181818", 3.0),
    ("focus-ring/panel", "#f5b21b", "#181818", 3.0),
    # Homepage, by day (app/templates/root.html).
    ("home-ink/enamel", "#14201a", "#e7ebe5", 4.5),
    ("home-ink-2/enamel", "#3d4a43", "#e7ebe5", 4.5),
    ("home-ink-3/enamel", "#56635b", "#e7ebe5", 4.5),
    ("home-ink-3/plate", "#56635b", "#f5f6f2", 4.5),
    ("home-primary-button", "#14201a", "#f2a900", 4.5),
    ("home-box-ink", "#e7ebe5", "#123b2c", 4.5),
    ("home-box-ink-2", "#a9c2b5", "#123b2c", 4.5),
    ("home-red/enamel", "#b8261a", "#e7ebe5", 3.0),
    ("home-green/enamel", "#1e7a47", "#e7ebe5", 3.0),
    ("home-blue/enamel", "#1f4e9e", "#e7ebe5", 3.0),
]


def luminance(value: str) -> float:
    value = value.lstrip("#")
    channels = [int(value[i : i + 2], 16) / 255 for i in (0, 2, 4)]

    def linear(channel: float) -> float:
        return channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4

    red, green, blue = (linear(channel) for channel in channels)
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def ratio(foreground: str, background: str) -> float:
    high, low = sorted((luminance(foreground), luminance(background)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def main() -> int:
    failed = False
    for name, foreground, background, minimum in PAIRS:
        measured = ratio(foreground, background)
        status = "PASS" if measured >= minimum else "FAIL"
        failed = failed or measured < minimum
        print(f"{status:4} {name:24} {measured:5.2f}:1 (min {minimum}:1)")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
