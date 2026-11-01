"""Public demo workspace and reusable template repository (growth task)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parent.parent


async def test_public_demo_is_read_only_and_discoverable(client):
    redirect = await client.get("/demo")
    assert redirect.status_code == 308
    assert redirect.headers["location"].endswith("/demo/")

    page = await client.get("/demo/")
    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    assert '<link rel="canonical" href="https://veynod.com/demo/">' in page.text
    assert "public read-only demo" in page.text
    assert "Open the console" in page.text
    assert "github.com/MuadibUsul/veynod/templates" in page.text
    assert "<form" not in page.text.lower() and "YOUR_VEYNOD_TOKEN" not in page.text

    sitemap = await client.get("/sitemap.xml")
    assert "https://veynod.com/demo/" in sitemap.text


def test_reusable_template_repository_exists():
    index = ROOT / "templates" / "README.md"
    template = ROOT / "templates" / "multi-agent-research"
    assert index.is_file() and "multi-agent-research" in index.read_text()
    for name in ("README.md", "agent-prompt.md", "workflow.md", "mcp.json"):
        assert (template / name).is_file(), name
    config = json.loads((template / "mcp.json").read_text())
    server = config["mcpServers"]["veynod"]
    assert server["url"] == "https://veynod.com/mcp/"
    assert server["headers"]["Authorization"] == "Bearer YOUR_VEYNOD_TOKEN"
