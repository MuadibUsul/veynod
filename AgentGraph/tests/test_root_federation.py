"""The homepage's federation chapter: three capabilities and a working demo."""

from __future__ import annotations

import re

from app.root_page import ROOT_PRIMITIVES, render_root_page

CAPABILITIES = {
    "en": (
        "Chat and delegate across workspaces",
        "Connections a person approves",
        "Reuse skills, borrow capacity",
    ),
    "zh": ("跨空间聊天与委派", "由人批准的连接", "复用 skill，借用额度"),
}


def _page(lang: str) -> str:
    descriptor = {
        "name": "Veynod",
        "version": "3.25.0",
        "dashboard": "/dashboard/",
        "first_call": "get_orientation",
        "primitives": list(ROOT_PRIMITIVES),
    }
    return render_root_page(descriptor, lang=lang)


def _chapter(page: str) -> str:
    match = re.search(r'<section class="chapter" id="federation">.*?</section>', page, re.S)
    assert match, "the federation chapter is missing"
    return match.group(0)


def test_federation_is_a_chapter_with_three_capabilities_not_a_teaser():
    for lang, other in (("en", "zh"), ("zh", "en")):
        page = _page(lang)
        chapter = _chapter(page)
        for title in CAPABILITIES[lang]:
            assert f"<h3>{title}</h3>" in chapter
        # The crawler-facing page carries one language only.
        for title in CAPABILITIES[other]:
            assert title not in page
        assert chapter.count("<h3>") == 3
        assert "Coming soon" not in page and "coming soon" not in page
        assert "即将推出" not in page
        assert 'class="soon' not in page


def test_the_demo_has_its_controls_and_a_live_note():
    for lang in ("en", "zh"):
        chapter = _chapter(_page(lang))
        for element_id in (
            "fed-step",
            "fed-step-label",
            "fed-block",
            "fed-reset",
            "fed-link",
            "fed-packet",
            "fed-log-acme",
            "fed-log-globex",
            "fed-state",
        ):
            assert f'id="{element_id}"' in chapter
        assert re.search(r'<p class="frame-note" id="fed-note" aria-live="polite">', chapter)
        # Blocking only appears once there is a connection to block.
        assert re.search(r'<button class="fed-block" id="fed-block" type="button" hidden>', chapter)


def test_the_demo_script_ships_with_the_page_in_both_languages():
    for lang in ("en", "zh"):
        page = _page(lang)
        script = page[page.index("// Federation: two workspaces, one handshake.") :]
        script = script[: script.index("</script>")]
        # Every step and the block path speak both languages; the page picks one.
        for phrase in (
            "Approve as Globex's owner",
            "以 Globex owner 身份批准",
            "Blocked",
            "已拉黑",
        ):
            assert phrase in script
        assert 'document.documentElement.dataset.lang === "zh"' in script


def test_the_demo_ids_are_unique_on_the_page():
    page = _page("en")
    for element_id in re.findall(r'id="(fed-[a-z-]+)"', page):
        assert page.count(f'id="{element_id}"') == 1, element_id
