"""Product UI convergence (ADR-018): one console, technical views internal."""

from __future__ import annotations

import httpx
import pytest


@pytest.mark.integration
@pytest.mark.parametrize(
    "path",
    ["/dashboard/code/", "/dashboard/agent-runs/", "/dashboard/projects/"],
)
async def test_former_technical_consoles_redirect_to_the_product(
    client: httpx.AsyncClient, path: str
) -> None:
    response = await client.get(path, follow_redirects=False)
    assert response.status_code == 302, response.text
    assert response.headers["location"] == "/dashboard/"


@pytest.mark.integration
async def test_the_product_page_has_no_technical_console_navigation(
    client: httpx.AsyncClient,
) -> None:
    # The Next.js export is served as a static shell; the navigation is built
    # in the exported bundle. Assert the backend serves the console at all, and
    # that the three product links no longer exist in the server-rendered shell.
    response = await client.get("/dashboard/")
    assert response.status_code == 200, response.text
    body = response.text
    for href in ("/dashboard/code/", "/dashboard/agent-runs/", "/dashboard/projects/"):
        assert href not in body, f"{href} is still a product navigation entry"
