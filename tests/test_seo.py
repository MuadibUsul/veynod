from __future__ import annotations

import xml.etree.ElementTree as ET
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
    assert f"{SITE}/docs/quickstart/" in urls
    assert f"{SITE}/docs/introduction/" not in urls
    for url in urls:
        assert url.startswith(f"{SITE}/")
        path = urlsplit(url).path
        for private in ("/dashboard", "/login", "/v1", "/mcp", "/oauth", "/cli"):
            assert not (path == private or path.startswith(private + "/")), url

    for url in urls:
        path = url.removeprefix(SITE)
        response = await client.get(
            path, headers={"Accept": "text/html"} if path == "/" else {}
        )
        assert response.status_code == 200, (url, response.status_code)
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
    assert api.headers["x-robots-tag"] == "noindex"
    assert api.headers["content-type"].startswith("application/json")


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


async def test_private_surfaces_send_noindex(client):
    for path in ("/dashboard/", "/dashboard/login/", "/v1/whoami", "/mcp/",
                 "/api-reference", "/healthz", "/readyz"):
        response = await client.get(path)
        assert response.headers.get("x-robots-tag") == "noindex, nofollow", path
