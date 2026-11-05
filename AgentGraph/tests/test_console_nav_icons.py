"""The console sidebar: which icon each view uses, and how they are drawn.

console-federation-sidebar-icon-v1: Federation used three joined nodes, like
the Diagram. It is now a globe with two hollow nodes. Every sidebar icon stays
a code component that draws inline SVG, never an image file.
"""

from __future__ import annotations

import re
from pathlib import Path

DASHBOARD = Path(__file__).resolve().parents[1] / "dashboard" / "app"
PAGE = (DASHBOARD / "page.tsx").read_text(encoding="utf-8")
UI = (DASHBOARD / "components" / "ui.tsx").read_text(encoding="utf-8")

# The sidebar as it was before this change, apart from Federation's drawing.
NAV = [
    ("diagram", "Diagram", "IconGraph"),
    ("loop", "Loop", "IconPulse"),
    ("federation", "Federation", "IconFederation"),
    ("documents", "Documents", "IconDocuments"),
    ("more", "Settings", "IconLayers"),
]


def _nav_items() -> list[tuple[str, str, str]]:
    block = PAGE[PAGE.index("const navItems:") : PAGE.index("const place = navItems")]
    return re.findall(
        r'\{ id: "([a-z]+)", label: t\("([^"]+)"\).*?icon: <(Icon[A-Za-z]+) />', block, re.S
    )


def _component(name: str) -> str:
    start = UI.index(f"export function {name}(")
    end = UI.index("\nexport function ", start + 1)
    return UI[start:end]


def test_each_view_keeps_its_name_order_and_icon():
    assert _nav_items() == NAV


def test_every_sidebar_icon_is_a_component_that_draws_inline_svg():
    for _, _, icon in NAV:
        body = _component(icon)
        assert "<svg" in body and 'stroke="currentColor"' in body, icon
        for banned in ("<img", "url(", ".svg", ".png", "base64", "background-image"):
            # url(#id) is a mask reference inside the same SVG, not a file.
            if banned == "url(":
                assert not re.search(r"url\((?!#)", body), icon
            else:
                assert banned not in body, (icon, banned)


def test_federation_is_a_globe_with_two_hollow_nodes():
    body = _component("IconFederation")
    assert '<circle cx="12" cy="12" r="8.25" />' in body
    assert "M3.75 12h16.5" in body  # the equator
    assert body.count("M12 3.75c") == 2  # two meridians
    for node in ('cx="19.2" cy="4.8" r="2.15"', 'cx="4.8" cy="19.2" r="2.15"'):
        assert node in body
    # The rim is cut by a mask, not painted over: no colour of its own.
    assert "mask={`url(#${cut})`}" in body and "useId()" in body
    assert "#0b0f14" not in body
    assert 'strokeWidth="1.5"' in body and 'strokeWidth="1.6"' not in body


def test_the_diagram_keeps_its_three_joined_nodes():
    body = _component("IconGraph")
    for node in ('cx="5" cy="6"', 'cx="19" cy="5"', 'cx="12" cy="18"'):
        assert node in body
    federation = _component("IconFederation")
    assert 'cx="5" cy="6"' not in federation and 'cx="12" cy="6"' not in federation
