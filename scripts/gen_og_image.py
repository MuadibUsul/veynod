"""Render the static 1200x630 social preview image at app/static/og.png.

Run after a brand or positioning change:

    python scripts/gen_og_image.py

The image is committed; the server only serves it, never renders it.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "app" / "static" / "og.png"
WIDTH, HEIGHT = 1200, 630

FONT_DIR = Path("C:/Windows/Fonts")
REGULAR = FONT_DIR / "segoeui.ttf"
SEMIBOLD = FONT_DIR / "seguisb.ttf"


def font(size: int, *, semibold: bool = False) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(SEMIBOLD if semibold else REGULAR), size)


def main() -> int:
    image = Image.new("RGB", (WIDTH, HEIGHT), "#0a0b0f")
    draw = ImageDraw.Draw(image, "RGBA")

    # A quiet grid and a violet glow behind the mark: the same visual language
    # as the landing page, without competing with the headline.
    for x in range(0, WIDTH, 60):
        draw.line((x, 0, x, HEIGHT), fill=(255, 255, 255, 6))
    for y in range(0, HEIGHT, 60):
        draw.line((0, y, WIDTH, y), fill=(255, 255, 255, 6))
    glow = Image.new("RGBA", (560, 560), (0, 0, 0, 0))
    glow_draw = ImageDraw.Draw(glow, "RGBA")
    for radius in range(260, 0, -8):
        alpha = int(26 * (1 - radius / 260) ** 1.6)
        glow_draw.ellipse(
            (280 - radius, 280 - radius, 280 + radius, 280 + radius),
            fill=(101, 80, 240, alpha),
        )
    image.paste(glow, (860, 60), glow)

    # Mark: a rounded board with the two strokes of the Veynod V.
    mark_x, mark_y, mark_size = 78, 66, 66
    draw.rounded_rectangle(
        (mark_x, mark_y, mark_x + mark_size, mark_y + mark_size),
        radius=18,
        fill="#141824",
        outline=(255, 255, 255, 32),
        width=1,
    )
    draw.line(
        (mark_x + 19, mark_y + 19, mark_x + 33, mark_y + 47),
        fill="#e8eaf0",
        width=7,
    )
    draw.line(
        (mark_x + 33, mark_y + 47, mark_x + 47, mark_y + 19),
        fill="#8b7cff",
        width=7,
    )

    draw.text((164, 78), "Veynod", font=font(40, semibold=True), fill="#e8eaf0")

    draw.text(
        (80, 206),
        "A shared blackboard",
        font=font(72, semibold=True),
        fill="#e8eaf0",
    )
    draw.text(
        (80, 292),
        "for agent teams.",
        font=font(72, semibold=True),
        fill="#b9afff",
    )
    draw.text(
        (82, 408),
        "Versioned memory, leases, quality gates",
        font=font(31),
        fill="#a3a8b6",
    )
    draw.text(
        (82, 451),
        "and human approval.",
        font=font(31),
        fill="#a3a8b6",
    )

    draw.line((80, 536, 1120, 536), fill=(255, 255, 255, 24), width=1)
    draw.text(
        (80, 560),
        "veynod.com",
        font=font(26, semibold=True),
        fill="#b9afff",
    )
    footer = "Agents do the work. Humans hold the line."
    footer_font = font(24)
    footer_width = draw.textlength(footer, font=footer_font)
    draw.text(
        (1120 - footer_width, 562),
        footer,
        font=footer_font,
        fill="#737a8b",
    )

    TARGET.parent.mkdir(parents=True, exist_ok=True)
    image.save(TARGET, "PNG", optimize=True)
    print(f"wrote {TARGET.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
