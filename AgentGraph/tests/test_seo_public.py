"""Public SEO routes do not require a database or an agent token."""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET

import httpx
import pytest

from app.main import app


@pytest.mark.asyncio
async def test_homepage_negotiation_and_locales(monkeypatch):
    async def no_release():
        return None

    monkeypatch.setattr("app.main._current_release", no_release)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        en = await client.get("/", headers={"Accept": "text/html,application/json;q=0.9"})
        assert en.status_code == 200
        assert en.headers["content-type"].startswith("text/html")
        assert "Accept" in en.headers["vary"]
        assert '<html lang="en"' in en.text
        assert 'href="/zh/" hreflang="zh-Hans"' in en.text
        assert "Shared blackboard for agents." in en.text
        assert "MCP control plane for AI teams." in en.text
        body = en.text.split("<body>", 1)[1]
        body = re.sub(r"<script\b[^>]*>.*?</script>", "", body, flags=re.S)
        assert not re.search(r"[\u4e00-\u9fff]", body.replace("中文", ""))

        generic = await client.get("/", headers={"Accept": "*/*"})
        assert generic.headers["content-type"].startswith("text/html")
        wildcard = await client.get("/", headers={"Accept": "application/*"})
        assert wildcard.headers["content-type"].startswith("text/html")
        weighted = await client.get(
            "/", headers={"Accept": "application/json;q=0.1,text/*;q=0.9"})
        assert weighted.headers["content-type"].startswith("text/html")
        mixed = await client.get(
            "/", headers={"Accept": "application/json, text/html"})
        assert mixed.headers["content-type"].startswith("text/html")
        wildcard_json = await client.get(
            "/", headers={"Accept": "application/json, */*"})
        assert wildcard_json.headers["content-type"].startswith("text/html")
        forced = await client.get("/?format=json", headers={"Accept": "text/html"})
        assert forced.headers["content-type"].startswith("application/json")
        api = await client.get("/", headers={"Accept": "application/json"})
        assert api.status_code == 200
        assert "Accept" in api.headers["vary"]
        assert "Accept-Encoding" in api.headers["vary"]
        assert api.json()["name"] == "Veynod"
        assert api.headers["x-robots-tag"] == "noindex, noarchive"

        zh = await client.get("/zh/", headers={"Accept": "application/json"})
        assert zh.status_code == 200
        assert "Accept" in zh.headers["vary"]
        assert '<html lang="zh-Hans"' in zh.text
        assert '<link rel="canonical" href="https://veynod.com/zh/">' in zh.text
        assert "共享黑板" in zh.text and "MCP 控制平面" in zh.text
        assert "Shared blackboard for agents." not in zh.text
        assert 'href="/" hreflang="en"' in zh.text

        for page in (en, zh):
            match = re.search(
                r'<script type="application/ld\+json">(.*?)</script>', page.text, re.S
            )
            assert match
            data = json.loads(match.group(1))
            types = {entry["@type"] for entry in data["@graph"]}
            assert {"SoftwareApplication", "FAQPage"} <= types


@pytest.mark.asyncio
async def test_public_crawler_endpoints_and_canonical_redirects():
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        for path in (
            "/robots.txt",
            "/sitemap.xml",
            "/llms.txt",
            "/llms-full.txt",
            "/docs/",
            "/v1/agent-docs",
        ):
            response = await client.get(path)
            assert response.status_code == 200, path
            assert "noindex" not in response.headers.get("x-robots-tag", ""), path
        sitemap = await client.get("/sitemap.xml")
        tree = ET.fromstring(sitemap.text)
        locations = {loc.text for loc in tree.iter() if loc.tag.endswith("loc")}
        assert "https://veynod.com/zh/" in locations
        assert "https://veynod.com/demo/" in locations
        assert "https://veynod.com/llms-full.txt" in locations
        assert 'hreflang="zh"' in sitemap.text and 'hreflang="x-default"' in sitemap.text
        robots = await client.get("/robots.txt")
        assert "Allow: /v1/agent-docs" in robots.text
        assert "Allow: /dashboard/_next/static/" in robots.text
        for asset in ("/og.png", "/favicon.ico", "/favicon.svg", "/apple-touch-icon.png"):
            head = await client.head(asset)
            assert head.status_code == 200, asset
        llms = await client.get("/llms.txt")
        assert "MCP" in llms.text and "Vyond" in llms.text and "/demo/" in llms.text
        full = await client.get("/llms-full.txt")
        assert "get_orientation" in full.text
        assert "npx veynod" in full.text
        assert len(full.text) > 30_000
        for host in ("www.veynod.com",):
            redirect = await client.get(
                "/docs/quickstart/?a=1", headers={"Host": host}, follow_redirects=False
            )
            assert redirect.status_code == 301
            assert redirect.headers["location"] == "https://veynod.com/docs/quickstart/?a=1"


@pytest.mark.asyncio
async def test_chinese_docs_have_their_own_urls_and_pairs(monkeypatch):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        zh = await client.get("/zh/docs/quickstart/")
        assert zh.status_code == 200
        assert "快速开始" in zh.text
        lowered = zh.text.lower()
        assert 'hreflang="en"' in lowered and "https://veynod.com/docs/quickstart/" in zh.text
        assert zh.headers["link"] == '<https://veynod.com/zh/docs/quickstart/>; rel="canonical"'
        en = await client.get("/docs/quickstart/")
        assert "https://veynod.com/zh/docs/quickstart/" in en.text
        # The old client-side switch moves permanently to the Chinese URL.
        old = await client.get("/docs/quickstart/?lang=zh", follow_redirects=False)
        assert old.status_code == 301 and old.headers["location"] == "/zh/docs/quickstart/"
        sitemap = (await client.get("/sitemap.xml")).text
        assert "<loc>https://veynod.com/zh/docs/quickstart/</loc>" in sitemap
        assert sitemap.count('hreflang="x-default"') >= 30


@pytest.mark.asyncio
async def test_landing_page_revalidates_and_trust_files_exist(monkeypatch):
    async def no_release():
        return None

    monkeypatch.setattr("app.main._current_release", no_release)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        first = await client.get("/", headers={"Accept": "text/html"})
        etag = first.headers["etag"]
        assert first.headers["cache-control"] == "public, max-age=0, must-revalidate"
        again = await client.get("/", headers={"Accept": "text/html", "If-None-Match": etag})
        assert again.status_code == 304
        api = await client.get("/", headers={"Accept": "application/json"})
        assert api.headers["cache-control"] == "no-store"
        h1 = re.search(r"<h1.*?</h1>", first.text, re.S).group(0)
        assert "agents. " in re.sub(r"<[^>]+>", "", h1)
        assert "loopruns" not in re.sub(r"<[^>]+>", "", first.text)
        for path in ("/.well-known/security.txt", "/site.webmanifest"):
            assert (await client.get(path)).status_code == 200, path
        missing = await client.get("/no-such-page", headers={"Accept": "text/html"})
        assert missing.status_code == 404
        assert missing.headers["content-type"].startswith("text/html")
        api_missing = await client.get("/v1/no-such-thing", headers={"Accept": "text/html"})
        assert api_missing.headers["content-type"].startswith("application/json")


@pytest.mark.asyncio
async def test_comparison_pages_bilingual_seo_and_cache():
    from app.vs_pages import COMPARISONS

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        for path in ("/vs/", "/zh/vs/"):
            hub = await client.get(path)
            assert hub.status_code == 200, path
            assert "etag" in hub.headers
            assert hub.headers["cache-control"] == "public, max-age=0, must-revalidate"
            reval = await client.get(path, headers={"If-None-Match": hub.headers["etag"]})
            assert reval.status_code == 304
        for item in COMPARISONS:
            en_path = f"/vs/{item.slug}/"
            zh_path = f"/zh/vs/{item.slug}/"
            en = await client.get(en_path)
            zh = await client.get(zh_path)
            assert en.status_code == 200 and zh.status_code == 200
            assert f"https://veynod.com{en_path}" in en.text
            assert f"https://veynod.com{zh_path}" in en.text
            assert '"@type": "FAQPage"' in en.text and '"@type": "FAQPage"' in zh.text
            assert "Veynod 对比" in zh.text
            redir = await client.get(f"/vs/{item.slug}", follow_redirects=False)
            assert redir.status_code == 308 and redir.headers["location"] == en_path
        unknown = await client.get("/vs/unknown-tool/", headers={"Accept": "text/html"})
        assert unknown.status_code == 404
        assert unknown.headers["content-type"].startswith("text/html")
        sitemap = (await client.get("/sitemap.xml")).text
        assert "<loc>https://veynod.com/vs/</loc>" in sitemap
        assert "<loc>https://veynod.com/zh/vs/t3-code/</loc>" in sitemap
