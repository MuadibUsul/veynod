# SEO ecosystem submission pack

Status as of 2026-10-29. This file is the implementation record for
`veynod-seo-ecosystem-registration`. It separates what the client can verify or
prepare from what only the owner can publish.

## Already verified

- GitHub repository: <https://github.com/MuadibUsul/veynod> — public, description
  and README point at <https://veynod.com>.
- npm package: `veynod@0.2.2`, `homepage` = <https://veynod.com>.
- Homepage `Organization.sameAs` now contains the GitHub repository and npm
  package only. Do not add a social or directory profile until it is live and
  verified.
- Every MCP tool now carries `title`, `readOnlyHint` and `destructiveHint`
  (112/112); the server advertises the hints on `tools/list`.

## Prepared for the owner (not submitted by an agent)

### 1. Official MCP Registry

The repository now contains a remote-only `server.json`:

- name: `io.github.muadibusul/veynod` (GitHub namespace; change the owner once
  `github.com/veynod` exists)
- remote: `https://veynod.com/mcp/`, Streamable HTTP
- required header: `Authorization: Bearer agt_…`

Owner steps (GitHub login owns the namespace):

```bash
# Install mcp-publisher (Windows, amd64)
$arch = if ([System.Runtime.InteropServices.RuntimeInformation]::ProcessArchitecture -eq "Arm64") { "arm64" } else { "amd64" }
Invoke-WebRequest -Uri "https://github.com/modelcontextprotocol/registry/releases/latest/download/mcp-publisher_windows_$arch.tar.gz" -OutFile "mcp-publisher.tar.gz"
tar xf mcp-publisher.tar.gz mcp-publisher.exe

./mcp-publisher.exe login github
./mcp-publisher.exe publish
```

If the namespace should be `com.veynod/veynod`, use DNS authentication and add
the TXT record the CLI prints for `veynod.com`; keep `io.github.muadibusul` as
the fast path until the GitHub organization is verified.

### 2. GitHub organization and domain verification

1. Create <https://github.com/veynod> (owner account).
2. Organization Settings → Verified & approved domains → add `veynod.com`.
3. Add the DNS TXT record GitHub prints (Namecheap → Advanced DNS).
4. After verification, change `server.json` `name` to `io.github.veynod/veynod`
   and replace the `sameAs` GitHub URL with the organization URL.

### 3. Directory listings (owner account, one-time)

| Directory | Action | Link |
|---|---|---|
| Glama | Claim the Veynod server entry | <https://glama.ai/mcp/servers> |
| Smithery | Submit via CLI, attach `https://veynod.com/docs` | <https://smithery.ai/new> |
| PulseMCP | Submit the server | <https://www.pulsemcp.com/submit> |
| mcp.directory | Submit the server | <https://mcp.directory/submit> |
| Cline marketplace | Open an issue with a 400×400 logo | <https://github.com/cline/mcp-marketplace> |
| Docker MCP Registry | PR against the registry repo | <https://github.com/docker/mcp-registry> |
| Cursor marketplace | Publish the remote server | <https://cursor.com/marketplace/publish> |
| Claude connectors | Add the remote connector | <https://claude.com/connectors> |

For every directory: README first line must be <https://veynod.com>; use the
same title/description as `server.json`.

### 4. Awesome lists

Draft entry (English):

> - [Veynod](https://veynod.com) — Shared blackboard and MCP control plane for
>   multi-agent teams: versioned memory, task hand-offs with leases, human
>   approval gates and delta context sync.

Open a PR to at least one of:

- <https://github.com/punkpeye/awesome-mcp-servers>
- <https://github.com/appcypher/awesome-mcp-servers>
- <https://github.com/e2b-dev/awesome-ai-agents>

### 5. Launch copy

Show HN title:

> Show HN: Veynod – A shared blackboard and MCP control plane for agent teams

Show HN body (draft):

> Multi-agent demos usually stop at a chat window. The hard part starts when
> Claude Code, Cursor and a custom worker touch the same files: lost context,
> duplicate work, no audit trail, and no way to stop a stuck agent.
>
> Veynod is a runtime-agnostic shared blackboard over MCP/REST. Agents share one
> versioned memory, hand off tasks with expiring leases, pass human approval
> gates, and pull only the delta since their last sync. The homepage is also a
> JSON API for agents: `Accept: application/json` returns the machine descriptor.
>
> It is live at https://veynod.com and the MCP endpoint is
> https://veynod.com/mcp/. I would value feedback on the lease/hand-off model and
> on what a human approval gate should look like for coding agents.

Reddit r/mcp post (draft):

> **Veynod: a shared blackboard for Claude Code, Cursor and custom agents**
>
> We built Veynod because multi-agent work kept failing at the hand-off: two
> agents edit one file, a worker dies and nobody knows what it touched, and the
> next agent starts from a stale summary. Veynod gives them one versioned
> blackboard over MCP, leases that expire when a worker vanishes, and a human
> approval gate before a change ships. The MCP endpoint is
> https://veynod.com/mcp/ and there is a public demo at
> https://veynod.com/demo/. Happy to answer questions about the lease model or
> the MCP tool surface.

### 6. Brand anchors (owner)

- X/Twitter <https://x.com/veynod>, LinkedIn company page, Crunchbase, F6S,
  Product Hunt maker page, AlternativeTo entry.
- After each profile is live, add its exact URL to the homepage
  `Organization.sameAs` and re-run `tests/test_root_page.py`.

### 7. GSC / Search Console (owner)

- Verify both `veynod.com` and the legacy test host.
- Submit Change of Address from the legacy host to `veynod.com`.
- Request indexing for `/`, `/zh/`, `/docs/`.

## Acceptance mapping

- A-01: prepared `server.json` and directory checklist; **not submitted** —
  publishing needs the owner's GitHub/registry account.
- A-02: npm homepage verified; GitHub org and at least one Awesome PR are
  owner-only; the copy above is ready to paste.
- A-03: `sameAs`, README URL and 112 tool annotations are in this change.
- A-04: owner-only list is this document.
- A-05: submitted-items list (currently GitHub repo + npm only), owner list and
  English copy drafts are above.
