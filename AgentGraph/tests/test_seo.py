from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urlsplit

import pytest

pytestmark = pytest.mark.integration

SITE = "https://veynod.com"
SITEMAP_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}


async def test_robots_and_sitemap_are_public_and_clean(client):
    robots = await client.get("/robots.txt")
    assert robots.status_code == 200
    assert robots.headers["content-type"].startswith("text/plain")
    assert "Allow: /" in robots.text
    assert f"Sitemap: {SITE}/sitemap.xml" in robots.text
    assert "Allow: /v1/agent-docs" in robots.text
    for private in ("/dashboard/", "/v1/", "/mcp/", "/api-reference",
                    "/openapi.json", "/healthz", "/readyz"):
        assert f"Disallow: {private}" in robots.text

    sitemap = await client.get("/sitemap.xml")
    assert sitemap.status_code == 200
    assert sitemap.headers["content-type"].startswith("application/xml")
    assert "public" in sitemap.headers["cache-control"]
    root = ET.fromstring(sitemap.text)
    urls = [loc.text for loc in root.findall("sm:url/sm:loc", SITEMAP_NS)]
    assert f"{SITE}/" in urls and f"{SITE}/docs/" in urls
    for public in ("/zh/", "/v1/agent-docs", "/llms.txt", "/llms-full.txt"):
        assert f"{SITE}{public}" in urls
    assert f"{SITE}/docs/quickstart/" in urls
    assert f"{SITE}/docs/introduction/" not in urls
    for url in urls:
        assert url.startswith(f"{SITE}/")
        path = urlsplit(url).path
        for private in ("/dashboard", "/login", "/mcp", "/oauth", "/cli"):
            assert not (path == private or path.startswith(private + "/")), url

    for url in urls:
        path = url.removeprefix(SITE)
        response = await client.get(
            path, headers={"Accept": "text/html"} if path == "/" else {}
        )
        assert response.status_code == 200, (url, response.status_code)
        if path.endswith(".txt"):
            assert response.headers["content-type"].startswith("text/plain"), url
        elif path == "/v1/agent-docs":
            assert response.headers["content-type"].startswith("application/json"), url
        else:
            assert response.headers["content-type"].startswith("text/html"), url


async def test_root_html_carries_search_metadata_and_json_is_noindex(client):
    page = await client.get("/", headers={"Accept": "text/html"})
    assert page.status_code == 200
    assert page.headers["link"] == f'<{SITE}/>; rel="canonical"'
    assert page.headers.get("x-robots-tag") is None
    assert f'<link rel="canonical" href="{SITE}/">' in page.text
    assert '<meta property="og:type" content="website">' in page.text
    assert f'<meta property="og:image" content="{SITE}/og.png">' in page.text
    assert '<meta name="twitter:card" content="summary_large_image">' in page.text
    assert '"@type": "SoftwareApplication"' in page.text
    assert '"@type": "Organization"' in page.text

    api = await client.get("/", headers={"Accept": "application/json"})
    assert api.status_code == 200
    assert api.headers["x-robots-tag"] == "noindex, noarchive"
    assert api.headers["content-type"].startswith("application/json")

    # Generic clients and crawlers receive HTML unless JSON is explicit.
    default = await client.get("/")
    assert default.headers["content-type"].startswith("text/html")
    assert default.headers.get("x-robots-tag") is None

    browser_accept = await client.get(
        "/", headers={"Accept": "text/html,application/json;q=0.9"}
    )
    assert browser_accept.headers["content-type"].startswith("text/html")

    for accept, expected in (
        ("application/json;q=0.1,text/*;q=0.9", "text/html"),
        ("text/*", "text/html"),
        ("application/*", "text/html"),
        ("application/json,text/html;q=0.9", "application/json"),
        ("application/json;q=0.1,*/*;q=0.9", "text/html"),
    ):
        response = await client.get("/", headers={"Accept": accept})
        assert response.headers["content-type"].startswith(expected), (accept, response.headers)

    forced = await client.get("/", params={"format": "json"},
                              headers={"Accept": "text/html"})
    assert forced.headers["content-type"].startswith("application/json")
    assert forced.headers["x-robots-tag"] == "noindex, noarchive"


async def test_crawlers_get_the_page_and_the_brand_is_in_its_text(client):
    """2026-11-05 audit: the logo read "eynod", the H1 had no brand, HEAD was 405."""
    for agent in ("Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
                  "Mozilla/5.0 (compatible; bingbot/2.0)", "GPTBot/1.1", "ClaudeBot/1.0"):
        # Even a crawler that asks for JSON gets the page it can index.
        page = await client.get("/", headers={"User-Agent": agent, "Accept": "application/json"})
        assert page.headers["content-type"].startswith("text/html"), agent
        assert page.headers.get("x-robots-tag") is None, agent
        assert "User-Agent" in page.headers["vary"] and "Accept" in page.headers["vary"]
    agent = await client.get("/", headers={"User-Agent": "veynod-cli/0.6.0",
                                           "Accept": "application/json"})
    assert agent.headers["content-type"].startswith("application/json")
    assert agent.headers["link"] == f'<{SITE}/>; rel="canonical"'

    for path in ("/", "/zh/", "/robots.txt", "/sitemap.xml", "/llms.txt"):
        head = await client.head(path)
        assert head.status_code == 200, path

    # One address per page: /zh redirects permanently to /zh/.
    zh = await client.get("/zh", follow_redirects=False)
    assert zh.status_code == 308 and zh.headers["location"] == "/zh/"

    # The text a crawler reads names the brand: the logo's link text and the H1.
    import re

    for path in ("/", "/zh/"):
        html_text = (await client.get(path, headers={"Accept": "text/html"})).text
        brand = re.search(r'<a class="brand"[^>]*>(.*?)</a>', html_text, re.S).group(1)
        assert re.sub(r"<[^>]+>", "", brand) == "Veynod", path
        h1 = re.sub(r"<[^>]+>", "", re.search(r"<h1.*?</h1>", html_text, re.S).group(0))
        assert "Veynod" in h1, path
        assert ".sr-only {" in html_text
        assert f'"@id": "{SITE}/#organization"' in html_text
        assert "https://github.com/MuadibUsul/veynod" in html_text


async def test_the_demo_is_followed_but_never_indexed(client):
    demo = await client.get("/demo/")
    assert demo.status_code == 200
    assert demo.headers["x-robots-tag"] == "noindex, follow"
    assert '<meta name="robots" content="noindex, follow">' in demo.text
    sitemap = await client.get("/sitemap.xml")
    assert f"{SITE}/demo/" not in sitemap.text


async def test_docs_pages_have_canonical_header_and_og_image(client):
    docs = await client.get("/docs/quickstart/")
    assert docs.status_code == 200
    assert docs.headers["link"] == f'<{SITE}/docs/quickstart/>; rel="canonical"'
    assert docs.headers.get("x-robots-tag") is None

    image = await client.get("/og.png")
    assert image.status_code == 200
    assert image.headers["content-type"].startswith("image/png")
    assert image.content.startswith(b"\x89PNG\r\n\x1a\n")
    assert len(image.content) > 10_000


async def test_favicon_and_apple_touch_icon_are_public(client):
    root = Path(__file__).resolve().parent.parent
    svg = (root / "app/static/favicon.svg").read_bytes()
    apple = (root / "app/static/apple-touch-icon.png").read_bytes()

    icon = await client.get("/favicon.svg")
    assert icon.status_code == 200
    assert icon.headers["content-type"].startswith("image/svg+xml")
    assert icon.content == svg

    touch = await client.get("/apple-touch-icon.png")
    assert touch.status_code == 200
    assert touch.headers["content-type"].startswith("image/png")
    assert touch.content == apple

    page = await client.get("/", headers={"Accept": "text/html"})
    assert '<link rel="icon" href="/favicon.svg" type="image/svg+xml">' in page.text
    assert '<link rel="apple-touch-icon" href="/apple-touch-icon.png">' in page.text
    assert "/dashboard/icon.svg" not in page.text
    assert "/dashboard/apple-icon.png" not in page.text

    robots = await client.get("/robots.txt")
    assert "Disallow: /dashboard/" in robots.text
    assert "/favicon.svg" not in robots.text
    assert "/apple-touch-icon.png" not in robots.text

    og = await client.get("/og.png")
    assert og.status_code == 200
    assert og.headers["content-type"].startswith("image/png")


async def test_private_surfaces_send_noindex(client):
    for path in ("/dashboard/", "/dashboard/login/", "/v1/whoami", "/mcp/",
                 "/api-reference", "/healthz", "/readyz"):
        response = await client.get(path)
        assert response.headers.get("x-robots-tag") == "noindex, nofollow", path
