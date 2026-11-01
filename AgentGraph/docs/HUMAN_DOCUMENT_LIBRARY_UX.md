# Human Document Library UX

**Status:** Proposed  
**Date:** 2026-10-25  
**Scope:** workspace shared documents in `/dashboard/` (Library tab) and the human search API

## 1. Problem

The console Library is designed like an editor with filters:

- namespace text field;
- verification status;
- taint state;
- sort order;
- updated-after/before dates;
- grouped document list.

It has **no full-text search**. PowerK currently holds over 1,000 documents, so a
human who remembers a phrase, a title fragment, or an approximate key has to
guess the namespace and page through the list. The editor also competes with
reading: selecting a document immediately shows edit controls and a properties
panel, while most human visits are to read, verify or link a document.

Human needs are different from agent needs:

- agents query by exact key, namespace and structured filters;
- people search by memory: "that pricing note", "the doc about lease expiry",
  "what Claude wrote yesterday".

## 2. Goals

- A human can find a document in seconds by title, key, body text or
  frontmatter, across all namespaces they can read.
- Search and reading are the primary modes; editing is secondary.
- Recent and pinned documents are one click away.
- Filters remain available but do not occupy the first screen.
- Results respect workspace permissions, namespace grants, taint and
  verification state.
- Chinese and English both work; no result should require remembering the
  exact key.

## 3. Non-goals

- Replacing the agent retrieval API or `/v1/query` contract.
- Making the workspace document library the public product documentation site
  (`/docs/`). They remain separate content sources.
- Building a general-purpose CMS, folders, or nested page hierarchy.
- Indexing documents the caller cannot read.

## 4. Information architecture

### Desktop

```text
┌──────────────────────────────────────────────────────────────────────┐
│ Search documents…                         Filters (2)   New document │
├───────────────┬──────────────────────────────────────────────────────┤
│ Namespaces    │  Results / reader                                     │
│ ▸ agentgraph  │  ┌────────────────────────────────────────────────┐   │
│ ▸ docs        │  │ Title                          v4  Verified     │   │
│ ▸ ops         │  │ namespace / doc_key · updated …                 │   │
│ ▸ research    │  │                                                │   │
│               │  │  Markdown reading view + table of contents     │   │
│ Recent        │  │                                                │   │
│ · …           │  └────────────────────────────────────────────────┘   │
│ Pinned        │                                                      │
│ · …           │                                                      │
└───────────────┴──────────────────────────────────────────────────────┘
```

- The search field is the first focusable control.
- Namespace rail is sticky; each namespace shows a count.
- “Recent” shows the last 5–8 opened documents.
- “Pinned” is a human-only shortcut, not a document property.
- The result list and reader share the main pane: results replace the reader
  while searching; selecting a result returns to the reader.
- Filters move behind a `Filters` button with an active-count badge.

### Mobile

- Search occupies the full width.
- Namespace rail becomes a drawer.
- Selecting a result opens the reader full-screen; back returns to results.
- Edit mode is a separate full-screen state.

## 5. Search behavior

### Fields

Rank in this order:

1. exact title;
2. title prefix / title contains;
3. exact or prefix `doc_key`;
4. frontmatter values;
5. body text;
6. semantic similarity (optional mode).

### Query semantics

- Default: all namespaces the caller may read.
- Typing filters immediately after a 250 ms debounce.
- Multiple words are ANDed for FTS; trigram matching covers partial and CJK
  terms.
- Quoted phrases are exact phrases.
- Search is case-insensitive.
- Archived documents are excluded by default; `include_archived` is a filter.
- Deprecated documents remain searchable with a visible badge.

### Result item

Each result shows:

- title;
- `doc_key` and namespace;
- one highlighted snippet with the matched field (`title`, `key`, `body`,
  `frontmatter`);
- version, verification, taint and lock badges;
- updated time and author.

No result should show a document the caller cannot dereference.

### States

- initial: recent + pinned + namespace overview;
- typing: skeletons, previous results kept until new ones arrive;
- empty: “No documents match” with namespace/filter reset;
- error: inline retry, not a toast that disappears;
- permission denied: the document is omitted, not shown as forbidden.

### Keyboard

- `/` focuses search unless an input is already focused;
- `Ctrl/Cmd+K` opens the same search as a command palette;
- `↑/↓` moves through results;
- `Enter` opens;
- `Esc` clears search / closes filters;
- `g` then `d` optionally jumps to the Library.

## 6. Reading view

Default state after selecting a document:

- reading layout with a maximum prose width of ~72 characters;
- title, namespace/key, version, verification, taint and lock in the header;
- Markdown body rendered without editor chrome;
- table of contents generated from headings;
- code blocks with copy buttons;
- version history behind “History”;
- “Edit” switches to the existing editor; unsaved edits prompt before leaving;
- related documents/backlinks if available;
- deep link: `/dashboard/?tab=documents&doc=<encoded-key>`.

Editing is not the default. For a human who opens the library to check a
handoff, the first screen should be readable.

## 7. Recent and pinned

MVP:

- recent is per browser/workspace in `localStorage`;
- pinned is per browser/workspace in `localStorage`;
- no schema change.

Phase 2 (cross-device):

- `human_document_visits (workspace_id, user_id, document_id, last_opened_at)`
  with RLS; upsert on open;
- `human_document_pins (workspace_id, user_id, document_id, created_at)`;
- both are human-only and never included in agent retrieval.

Recent must record only documents the user can currently read; when access is
removed, the entry is filtered out.

## 8. Search API

Add a human-only endpoint:

```text
GET /v1/human/documents/search
  ?q=<text>
  &namespace=<optional>
  &status=<optional>
  &tainted=<optional>
  &include_archived=false
  &mode=auto|keyword|semantic
  &sort=relevance|updated_desc|updated_asc
  &cursor=<opaque>
  &limit=20
```

Response:

```json
{
  "items": [
    {
      "doc_key": "ops/lease-expiry",
      "title": "Lease expiry",
      "namespace": "ops",
      "snippet": "…lease expiry is reaped after…",
      "matched_in": "body",
      "score": 0.82,
      "version": 4,
      "verification_status": "VERIFIED",
      "tainted": false,
      "locked_by_human": true,
      "updated_at": "...",
      "updated_by": "agent:analyst"
    }
  ],
  "next_cursor": null,
  "total_estimate": 1
}
```

Implementation notes:

- `q` is optional; without it the endpoint behaves like the existing list.
- Use PostgreSQL `tsvector` + `websearch_to_tsquery` for English and
  `pg_trgm` similarity/ILIKE for CJK and partial keys.
- Reuse the existing document visibility rules: workspace RLS, namespace grants,
  archived filter, taint and verification flags.
- Return `total_estimate` only when cheap; otherwise omit and use
  `next_cursor`.
- `mode=semantic` reuses `document_chunks` embeddings and can be slower; it is
  opt-in, never the default.
- Limit `q` to 200 characters; reject empty whitespace.
- Target P95 < 300 ms for keyword search over 1,000 documents.

Suggested indexes:

```sql
CREATE INDEX IF NOT EXISTS idx_documents_search_fts
  ON shared_documents
  USING GIN (to_tsvector('simple', coalesce(title,'') || ' ' || content));

CREATE INDEX IF NOT EXISTS idx_documents_title_trgm
  ON shared_documents USING GIN (title gin_trgm_ops);

CREATE INDEX IF NOT EXISTS idx_documents_key_trgm
  ON shared_documents USING GIN (doc_key gin_trgm_ops);
```

If a generated `search_vector` column is preferred, add it in the migration and
keep the indexes on that column.

## 9. Security and privacy

- Search uses the same visibility predicate as `dereference`; no separate
  search-only bypass.
- Tainted, draft, archived and other unverified documents follow the same
  default-visibility rules as the document list.
- Agent tokens cannot call the human search endpoint.
- Snippets must not include content from a document the caller cannot read.
- Operator privacy remains in force: the operator sees counts, not content,
  unless support access is open.
- Recent/pinned entries are per user and never leak across workspaces.

## 10. Acceptance criteria

1. With 1,000+ documents, searching a distinctive body phrase returns the
   document in the first page.
2. Searching a partial `doc_key` or a Chinese phrase works without knowing the
   namespace.
3. Search results never include a document outside the caller's readable
   namespaces.
4. Selecting a result opens a reading view, not the editor.
5. `Filters` shows the active filter count and can be collapsed; the first
   screen contains only search, namespaces and recent/pinned.
6. `Ctrl/Cmd+K`, `/`, arrow keys and `Enter` all work.
7. Recent documents survive a page reload in the same browser/workspace.
8. Deep links open the correct document and tab.
9. A deprecated document is searchable with a visible badge; an archived
   document is hidden unless `include_archived=true`.
10. Search P95 stays under 300 ms for keyword mode on 1,000 documents.
11. The public `/docs/` site and the workspace Library remain visually and
    conceptually separate.

## 11. Rollout

1. **Search + fewer filters:** backend endpoint, search bar, filter drawer,
   local recent. This is the highest-value change.
2. **Reader-first:** reading layout, TOC, metadata header, deep links.
3. **Pins and cross-device recent:** server-side per-user tables.
4. **Command palette and semantic mode:** reuse the existing retrieval stack.

## 12. Metrics

- time from opening Library to opening a document;
- search success rate (a result opened / searches with a query);
- zero-result rate;
- P95 search latency;
- recent/pin usage;
- editor opened directly from the list (should drop once reading is default).

## 13. Open decisions

- Whether recent should be server-side from day one (recommended for
  cross-device, but adds a table).
- Whether pinned documents belong in a dedicated table or a generic favorites
  table shared with agents.
- Whether search snippets are generated in SQL (`ts_headline`) or Python.
- Whether the Library should live at `/dashboard/documents` instead of a tab
  query parameter.
