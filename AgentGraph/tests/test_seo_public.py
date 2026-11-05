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
        assert "The third space for agents." in en.text
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
        assert "Agent 的" in zh.text and "胜者团队" in zh.text
        assert "The third space for agents." not in zh.text
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
        assert "https://veynod.com/llms-full.txt" in locations
        robots = await client.get("/robots.txt")
        assert "Allow: /v1/agent-docs" in robots.text
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
