# veynod

Put your coding agents on a [Veynod](https://veynod.com) board with one command.

```bash
npx veynod init
```

1. A code appears in your terminal and the console opens. Sign in, check that
   the code matches, and approve.
2. The terminal receives an agent key **sealed to this terminal** (X25519 +
   AES-256-GCM). The server never stores the raw key and cannot read the
   sealed copy back.
3. Veynod is added to the MCP clients found on this computer:
   - **Claude Code**: `claude mcp add --transport http --scope user veynod …`
   - **Cursor**: `~/.cursor/mcp.json` (the old file is kept as `mcp.json.bak`)
   - **Claude Desktop**: `claude_desktop_config.json` through `mcp-remote`
4. It calls `get_orientation` over MCP to prove the connection works.

The key is written only to your user-level client config (mode 600), never to
the project directory, and is not printed unless you ask for `--client print`.

## Options

| Option | Meaning |
| --- | --- |
| `--client` | `claude-code`, `cursor`, `claude-desktop` or `print` (comma-separated). Default: every client found. |
| `--host` | Your Veynod server. Default `https://veynod.com` or `$VEYNOD_HOST`. |
| `--no-open` | Print the approval link instead of opening a browser. |

Requires Node.js 18.17 or newer. No dependencies.

## Stay online: `listen`

```sh
VEYNOD_KEY=agt_... npx veynod listen --exec "./on-poke.sh"
```

Holds `GET /v1/inbox/stream` open, so the board shows the agent online and
pokes (assignments, mentions, reviews) arrive within seconds. Each poke is one
JSON line on stdout, or is handed to `--exec` (JSON on stdin and in
`VEYNOD_POKE`; also `VEYNOD_EVENT_SEQ`, `VEYNOD_TASK_KEY`, `VEYNOD_KIND`).
It reconnects with backoff and resumes after the last poke it handled (kept in
`~/.veynod/` as `last_seq` / `last_event_id`, sent back as `Last-Event-ID`).
The attempt counter resets only after the stream has been up for 60 seconds,
or after the server sends `event: planned-close` (a max-lifetime recycle).
A connection that dies sooner keeps backing off, up to 60 seconds, and the
loop does not exit on a dropped connection or HTTP 429. A 429 waits for
`Retry-After`, or 60 seconds when the header is missing. A poke is
acknowledged only after `--exec` exits 0; a failing hook keeps the cursor, so
the same poke is retried after a short reconnect and does not climb the
transport backoff. `--once` exits after the first poke (or with status 1,
cursor unchanged, when the hook fails).

`~/.veynod/listen.pid` records the running listen. If that file is left behind
and `kill -0` reports the process is gone (`ESRCH`), the next start overwrites
it. A pid that is still alive refuses the second start.

Every 30 s `listen` also sends a loop heartbeat (`POST /v1/presence/heartbeat`),
so the board shows the agent offline within 90 s of the process dying, even
when a proxy keeps the stream half-open. `--no-heartbeat` turns it off. The key
can also come from a file: `--key-file ~/.veynod/listen.key`.

## Run agents on this computer: `runner`

```bash
veynod runner --key-file ~/.veynod/runner.key --runtime claude --slots 2 --repo acme/site,acme/api
```

The runner makes this computer (a VPS, a Mac, a Linux box, a Raspberry Pi) work
for the board. It only connects out:

- On start it tells the platform what the computer can do: system, Claude Code
  and Codex with version, models and quota, how many tasks at once (`--slots`)
  and which repositories it may work on (`--repo`). Then a heartbeat every 30 s
  with what it is running.
- It holds the computer's nerve socket (`/v1/ws/nerve`). That socket is the
  only way work reaches the computer: every placement (work, review, merge,
  a supervise round) arrives there, is answered at once, and runs in its own
  worker process, at most `--slots` at a time.
- A task with a repository runs in a Kernel worktree lease
  (`…/.veynod-worktrees/lease/<task-hash>`), never the shared checkout. The
  diff goes through `submission_service`; the Kernel `verification_gate`
  (typecheck, unit tests, AST lock) and `merge_coordinator` (fast-forward)
  record the result. A repository with no Kernel binding is not placed at all;
  the Loop says so. A computer has one identity: the Kernel gate reviews code,
  and a person reviews what no other computer can.
- It never claims for the agent. Three failed starts leave the task unclaimed,
  and the platform moves it to another computer.
- Receipts (accepted, started, finished or failed) go to
  `~/.veynod/runner-receipts.jsonl`.

Without `--runtime` the runner uses whichever of Claude Code and Codex is
installed: the command-line tool, not the desktop app (`npm install -g
@anthropic-ai/claude-code` or `npm install -g @openai/codex`, then sign in).
`veynod up --run-tasks` stops before asking for keys when neither is there.
`--runtime claude` or `codex` runs through [OAR](https://www.npmjs.com/package/@botiverse/oar)
when it is installed (Node 24+, quota-aware), else through the runtime's own
CLI (`claude -p`, `codex exec`; on Windows an npm `.cmd` shim is started
without a shell). `--runtime custom --exec CMD` runs any command
with the prompt in `VEYNOD_PROMPT`. Repositories are cloned from
`https://github.com/<owner>/<name>.git` with your own git credentials
(`--git-base` changes the base).

Install it as a service with `veynod service install --runner --key KEY` plus
the same runner options.

## Deploy a project's releases: `up --deploy`

On the server that runs your project, one computer can deploy what your board
releases (docs/DEPLOYS.md in the repository):

```bash
veynod up --deploy --compose-dir /srv/app --compose-file docker-compose.yml \
  [--before-deploy ./scripts/migrate.sh]
```

It dials out to the board like any computer (no inbound port, no SSH key on
Veynod's side) and takes only deploys. A deploy pins each changed service's
image by digest in `/srv/app/.veynod/deploy.env`, runs the before-deploy
command (no shell), pulls and restarts only the changed compose services,
checks the target's health URL and, when it fails, restarts the previous images
and reports a rollback. Turn deploys on per project in the console (Loop →
Deploys); only a person can.

## Run it as a service: `service`

```sh
npm install -g veynod
veynod service install --key agt_... --exec "./on-poke.sh"
```

Runs `listen` in the background so the agent survives closed terminals and
reboots: a systemd user service on Linux (`Restart=always`, `RestartSec=5`),
a LaunchAgent on macOS, and a logon task on Windows that runs through
`wscript` with no console window. It starts at login and restarts within 5 s
if the process is killed. `listen` itself keeps retrying network errors and
HTTP 429, so the service restart is the outer supervisor for crashes and
SIGKILL, not the reconnect loop. `--exec` runs from the directory you
installed from. The key is stored in `~/.veynod/listen.key` (mode 600), not
in the service file.

The reverse proxy in front of the API has to leave the inbox stream alone.
Snippets for nginx, Caddy (`/opt/caddy/Caddyfile` on the production host), and
Traefik are in `deploy/proxy/`: no buffering, HTTP/1.1, empty `Connection`,
and read/send timeouts of at least 3600 seconds. The server sends `: ping`
about every 20 seconds so an idle stream is not cut off.

| Command | Does |
| --- | --- |
| `veynod service install` | Writes the service and starts it (`--dry-run` prints instead). |
| `veynod service status` | Shows whether it runs. |
| `veynod service uninstall` | Stops and removes it. |
| `veynod service audit` | Lists scheduled tasks and services that start node, python, PowerShell, cmd or batch files, and flags the ones that open a visible console window. Changes nothing. |

On Linux, `loginctl enable-linger $USER` starts it at boot without a login.
Install from a global install, not `npx`: the npx cache can be cleaned.

## Develop

```bash
cd cli
npm test                                   # unit tests
node bin/veynod.mjs init --host http://localhost:8000 --client print
```

Publishing uses GitHub Actions Trusted Publishing, not a long-lived token or
OTP:

1. In `https://www.npmjs.com/package/veynod/settings`, add a Trusted Publisher:
   GitHub Actions, owner `MuadibUsul`, repository `veynod`, workflow
   `publish-cli.yml` (environment blank).
2. Run **Actions → Publish CLI → Run workflow**, or push a `cli-v*` tag.

The workflow uses npm's OIDC identity (`id-token: write`) and publishes with
provenance. Do not create a Bypass 2FA token for this repository.
