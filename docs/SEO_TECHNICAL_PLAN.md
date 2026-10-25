# SEO Technical Plan — Global English First

**Status:** Proposed  
**Date:** 2026-10-25  
**Decisions:** English-first; root `/` defaults to HTML; legacy domain to be retired; technical SEO before content production

## 1. Executive summary

Veynod's public content is technically indexable but the site is missing the
signals search engines and social platforms need:

- `/robots.txt` and `/sitemap.xml` return a JSON 404;
- `/` returns the API descriptor when a crawler sends `Accept: */*`;
- the landing page has a title and description but no canonical, Open Graph,
  Twitter Card or JSON-LD;
- docs pages have unique titles/descriptions but no canonical, social metadata,
  hreflang or structured data;
- the legacy domain still serves a duplicate 200 site;
- dashboard and API pages are not explicitly noindex;
- Chinese content is only available through a client-side language toggle.

Phase 0 fixes these before any content investment. English is the primary SEO
market; Chinese URLs and hreflang are a later phase.

## 2. Current state (audit 2026-10-25)

| Area | Current | SEO impact |
| --- | --- | --- |
| `robots.txt` | 404 JSON | no crawl guidance, no sitemap reference |
| `sitemap.xml` | 404 JSON | docs pages discovered slowly or not at all |
| `/` with `Accept: */*` | API JSON descriptor | generic crawlers/link previews see no landing page |
| `/` with `text/html` | landing page | content is rendered; title/description present |
| Canonical | absent | duplicate/query URLs and legacy domain are consolidated poorly |
| Open Graph / Twitter | absent | poor link previews and no social discovery |
| JSON-LD | absent | no Organization/SoftwareApplication/Article context |
| Docs metadata | unique title + description | good, but missing canonical/social/hreflang |
| Docs content | server-rendered HTML | indexable (good) |
| Legacy domain | 200 duplicate | splits authority, duplicate content |
| `www` | 301 to apex | correct |
| HTTP | 308 to HTTPS | correct |
| `/dashboard/*` | no robots directive | private pages can be indexed |
| Language | EN/中文 via client-side storage | Chinese content lacks distinct indexable URLs |

## 3. Goals and non-goals

### Goals

- Every public canonical page is discoverable, unique and shareable.
- Search engines see the HTML landing page and the docs, never the API JSON.
- Private product surfaces are excluded from indexing.
- The legacy domain stops competing with `veynod.com`.
- Core Web Vitals are measured and kept within budget.
- The technical foundation is ready before content production starts.

### Non-goals (this phase)

- Chinese `/zh/` routes and `hreflang` (English-first; later phase).
- A blog or content marketing program (P2).
- Paid links, bulk directory submissions or keyword stuffing.
- Changing the product's information architecture beyond docs navigation.
- Indexing operator, customer workspace or MCP surfaces.

## 4. Phase 0 — technical SEO

### P0-1. robots.txt and sitemap.xml

Add public routes:

```text
GET /robots.txt
GET /sitemap.xml
```

`robots.txt`:

```text
User-agent: *
Allow: /
Disallow: /dashboard/
Disallow: /v1/
Disallow: /mcp/
Disallow: /api-reference
Disallow: /openapi.json
Disallow: /healthz
Disallow: /readyz
Sitemap: https://veynod.com/sitemap.xml
```

`sitemap.xml`:

- `https://veynod.com/`
- every public docs page from `docs_site.build()` / `site.nav`;
- absolute URLs, `<lastmod>` from the docs build date or page source date;
- no `/dashboard`, `/login`, `/api`, `/mcp`, `/v1`, `/oauth`, `/cli`;
- `Cache-Control: public, max-age=300` (regenerated on deploy).

Acceptance: both return 200; every sitemap URL returns 200 HTML; no private URL
appears.

### P0-2. Root content negotiation

Current behavior:

| `Accept` | Current |
| --- | --- |
| `text/html` | HTML |
| `application/json` | JSON |
| `*/*` | JSON |
| `?format=json` | JSON |

Target behavior:

| Request | Response |
| --- | --- |
| `Accept: text/html` | HTML |
| `Accept: */*` | **JSON** (product decision: agents call `/` directly) |
| `Accept: application/json` | JSON |
| `Accept: text/html, application/json;q=0.9` | HTML |
| `?format=json` | JSON |

Implementation:

- Treat an explicit `application/json` (with a higher preference than HTML) or
  `?format=json` as the JSON opt-in.
- Keep JSON as the default for `*/*` and absent `Accept`: this is an API
  endpoint for agents, and changing it would break existing clients. Search
  engines send `text/html` and receive the landing page.
- Keep `Vary: Accept`.
- Add `Link: <https://veynod.com/>; rel="canonical"` to HTML.
- Add `X-Robots-Tag: noindex` to JSON responses.
- Update Agent docs: use `/v1` or `/mcp/`; if the root descriptor is needed, use
  `?format=json` or explicit `Accept: application/json`.

Acceptance: the matrix above is covered by tests; `Accept: text/html` and
search-engine crawlers receive HTML, while a generic
`curl -H 'Accept: */*' https://veynod.com/` still receives the JSON root. The
JSON response is noindex.

### P0-3. Canonical, Open Graph and Twitter Cards

Landing page (`app/templates/root.html`):

```html
<link rel="canonical" href="https://veynod.com/">
<meta property="og:type" content="website">
<meta property="og:site_name" content="Veynod">
<meta property="og:title" content="Veynod — A shared blackboard for agent teams">
<meta property="og:description" content="...">
<meta property="og:url" content="https://veynod.com/">
<meta property="og:image" content="https://veynod.com/og.png">
<meta name="twitter:card" content="summary_large_image">
```

Docs pages:

- canonical: `https://veynod.com/docs/<slug>/` (home: `/docs/`);
- `og:type=article`, `article:modified_time`;
- title formula: `<Page title> · Veynod Docs`;
- description from the generated `summary`;
- same OG image (or a docs-specific one later).

Create one static `og.png` (1200×630) with the Veynod mark, title and domain.
Do not generate it per request.

Acceptance: canonical/OG/Twitter present on `/` and every docs page; OG image
returns 200 `image/png`; social preview debuggers resolve absolute URLs.

### P0-4. Structured data

Inject JSON-LD server-side:

- `/`: `Organization`, `SoftwareApplication`, `WebSite`;
- docs: `TechArticle` + `BreadcrumbList`;
- `/docs/faq/`: `FAQPage` only if the visible page contains the same Q&A.

Guidelines:

- `Organization`: name `Veynod`, url, logo, sameAs GitHub/npm;
- `SoftwareApplication`: name, applicationCategory `DeveloperApplication`,
  operatingSystem `Web`, `isAccessibleForFree: true` if accurate;
- no fabricated ratings, review counts, prices or customer logos;
- `WebSite` search action only after docs search exists.

Acceptance: JSON-LD parses; Rich Results Test shows no errors for the selected
types.

### P0-5. Private surfaces are not indexable

Add `X-Robots-Tag: noindex, nofollow` to:

- `/dashboard/*` (including login, connect, oauth, cli, growth);
- `/v1/*`, `/mcp/*`, `/api-reference`, `/openapi.json`;
- `/healthz`, `/readyz`.

Keep robots.txt disallow as a crawl-budget measure; the header is the
authoritative noindex signal. Do not rely on robots.txt alone to hide a page
from the index.

Acceptance: no private URL appears in the sitemap; headers are present on the
listed paths; Google can still fetch the public landing/docs.

### P0-6. Legacy domain retirement

User decision: the legacy domain can be shut down. Do it in this order:

1. Inventory clients still using `agentgraph.fusionpropulsion.space`:
   ZCode, Cursor, external MCP clients, docs links, agent configs.
2. Update known MCP URLs to `https://veynod.com/mcp/` and re-test.
3. Caddy: 301 all legacy paths to `https://veynod.com{uri}`.
4. Keep the legacy DNS record and certificate until the redirect has been
   active for at least 30 days.
5. Use Google Search Console's Change of Address for the legacy domain.
6. Monitor legacy 301s and MCP connection failures for 30 days.

Transitional option if some MCP clients cannot follow redirects: 301 HTML pages
but keep `/mcp/` and `/v1/` reverse-proxied for a short grace period. Do not
leave both full sites serving 200 long-term.

Acceptance: legacy `GET /` returns 301 to the new domain; no duplicate 200
landing page; old-domain MCP traffic is either migrated or explicitly
redirected.

### P0-7. Core Web Vitals

Baseline and budget:

- run PageSpeed Insights / Lighthouse on `/` and `/docs/quickstart/`;
- target LCP < 2.5 s (mobile), INP < 200 ms, CLS < 0.1;
- self-host or preload the Inter/JetBrains Mono subsets;
- reduce/disable the landing canvas animation on small screens and
  `prefers-reduced-motion`;
- keep third-party scripts to fonts and Google Identity Services only where
  required;
- set immutable caching for hashed `/_next/static/*` assets;
- avoid layout shifts from late-loading fonts/images.

Acceptance: a before/after Lighthouse record is committed under
`docs/benchmarks/seo/`; no metric regresses after Phase 0.

### P0-8. Search Console and analytics

- Verify `veynod.com` in Google Search Console (DNS TXT or HTML meta).
- Submit `/sitemap.xml`; monitor Coverage and Enhancements.
- Verify in Bing Webmaster Tools and submit the same sitemap.
- Add GA4 or Plausible with a documented privacy stance.
- Define events: docs page view, quickstart MCP connect, signup, agent
  orientation.

Acceptance: sitemap status "Success"; no manual actions; baseline organic
impressions/clicks recorded.

## 5. Phase 1 — docs architecture and internal links

After P0:

- a docs landing page organized by task ("connect an agent", "run a task",
  "review work", "operate a workspace"), not only by chapter;
- breadcrumbs on every docs page;
- related pages and previous/next navigation;
- unique descriptions and modified timestamps;
- docs search (client-side index first, API later);
- internal links from landing to the highest-intent docs pages;
- English-only canonical URLs; no `/zh/` yet.

## 6. Phase 2 — content clusters (after P0/P1)

English-first, 8–10 substantial pages:

- MCP server for AI agents
- Claude Desktop / Cursor MCP setup
- Human-in-the-loop agent orchestration
- Multi-agent task handoff with leases
- Agent memory with provenance
- Audit trail for AI agents
- Quality gates for agent output
- Agent blackboard vs. chat / vector databases
- Agent token and connection lifecycle
- Workspace security and operator privacy

Each page must contain code, a real workflow or a measured benchmark. No
auto-generated thin pages. Chinese pages come after English pages rank and the
`/zh/` route/hreflang work is scheduled.

## 7. Phase 3 — authority and measurement

- GitHub, npm, MCP directories, Claude/Cursor ecosystem listings;
- launch posts and reproducible benchmarks (e.g. `workspace_sync` 228 KB → 42 KB);
- case studies only with permission;
- quarterly review of GSC queries, index coverage, CTR and organic signups;
- monitor Core Web Vitals and broken internal links.

## 8. Acceptance checklist

- [x] `/robots.txt` 200, references the sitemap
- [x] `/sitemap.xml` 200, only public URLs, all return 200
- [x] `Accept: */*` on `/` keeps returning JSON (agent contract; HTML is served
      for `Accept: text/html`)
- [x] JSON root remains available via explicit Accept or `?format=json`, and is
      marked `X-Robots-Tag: noindex`
- [x] canonical/OG/Twitter on `/` and every docs page
- [x] valid JSON-LD on landing, docs and FAQ
- [x] noindex headers on dashboard/API/MCP/health pages
- [x] legacy domain 301s to veynod.com; no duplicate 200 landing
- [ ] Search Console verified; sitemap submitted; Bing verified
- [x] Lighthouse/CWV baseline recorded; no regression (mobile 97 / desktop 95
      performance, 100 SEO; `docs/benchmarks/seo/2026-10-25.md`)
- [x] Agent docs unchanged: root JSON contract retained for agents
- [x] No Chinese `/zh/` or hreflang in this phase

## 9. Risks and mitigations

| Risk | Mitigation |
| --- | --- |
| Agent clients depend on root JSON with `*/*` | retained unchanged; HTML is content-negotiated on `text/html`; JSON is noindex |
| Legacy MCP clients cannot follow 301 | inventory first; transitional `/mcp/` proxy window |
| Sitemap includes private pages | generate from the docs nav allowlist, never from route enumeration |
| OG image missing/slow | one static 1200×630 PNG served from `/og.png` |
| CWV regression from fonts/animation | self-host/subset fonts; reduce canvas on mobile |
| Structured data mismatch | derive JSON-LD from visible page fields only |
| Search Console access unavailable | DNS TXT verification or a temporary HTML verification file |

## 10. Implementation map

- `app/main.py` — root negotiation, noindex middleware/headers, robots/sitemap
  routes, canonical link header.
- `app/templates/root.html` — canonical, OG/Twitter, JSON-LD.
- `app/docs_site.py` / `dashboard/app/docs/` — docs canonical, social metadata,
  TechArticle/Breadcrumb JSON-LD.
- `scripts/gen_docs_site.py` — include sitemap data if generated at build time.
- `dashboard/app/docs/site.generated.json` — source of docs slugs/titles for the
  sitemap.
- Caddy — legacy domain 301 and static cache headers.
- `docs/benchmarks/seo/` — Lighthouse before/after records.

## 11. Definition of done

Phase 0 is done when a search engine can:

1. fetch `robots.txt` and `sitemap.xml`;
2. crawl the HTML landing page with `Accept: text/html` (what search engine
   crawlers send) while the `*/*` JSON agent contract stays intact;
3. discover every public docs page from the sitemap;
4. read canonical and structured data on each page;
5. never index dashboard/API/MCP pages;
6. see only `veynod.com` as the canonical host;
7. load the pages within the CWV budget.
