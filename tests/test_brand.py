"""Every drawn copy of the Veynod V matches the vector source in docs/brand/."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _paths(text: str) -> list[str]:
    return re.findall(r'\bd="([^"]+)"', text)


SOURCE = _paths((ROOT / "docs/brand/mark.svg").read_text(encoding="utf-8"))


def test_the_source_has_the_body_and_the_cut():
    assert len(SOURCE) == 2


def test_every_copy_draws_the_same_v():
    copies = [
        "docs/brand/mark-inverse.svg",
        "docs/brand/icon-dark.svg",
        "docs/brand/icon-light.svg",
        "docs/brand/wordmark.svg",
        "app/static/favicon.svg",
        "dashboard/app/icon.svg",
        "app/templates/root.html",
        "dashboard/app/components/ui.tsx",
    ]
    for copy in copies:
        drawn = _paths((ROOT / copy).read_text(encoding="utf-8"))
        for path in SOURCE:
            assert path in drawn, f"{copy} does not draw {path}"
