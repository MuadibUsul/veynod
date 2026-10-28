# Veynod Desktop (Windows, v1)

A Tauri shell around the existing web console. It stays in the tray, keeps one
SSE stream per configured agent (so they show online), and raises a Windows
notification only for actionable pokes.

## What v1 does

- Loads `https://veynod.com/dashboard/` in a native window; closing the window
  hides it to the tray instead of quitting.
- Tray menu: Open Veynod / Reload console / Quit.
- Enables launch at login (`--hidden`).
- Holds `GET /v1/inbox/stream` for every configured agent, so presence is real
  while the app runs.
- Notifies only for `task.assigned`, `task.review_rejected`, `task.escalated`,
  `task.awaiting_human`, `task.review_requested`, `mention.created` and
  `message.unread`; heartbeat/lease/status noise is ignored. The first 12 s of
  a first connection are silent (avoids replaying the unacked backlog), and
  notifications are rate-limited to one per 4 s.

## Configuration

`%USERPROFILE%\.veynod\desktop.json`:

```json
{
  "agents": ["zcode", "muse-dou-tinker"],
  "host": "https://veynod.com"
}
```

Each agent's raw key is read from `%USERPROFILE%\.veynod\<agent>.key` (one line,
already the convention used by `veynod listen`). Missing keys simply leave that
agent offline and are logged; the app does not invent credentials.

Cursor state is kept in `%USERPROFILE%\.veynod\desktop-state.json` so a restart
resumes from the last delivered event.

## First run

The window now opens a local setup wizard before the console:

1. **Check this computer** runs the OAR sidecar inventory (`desktop/sidecar/bridge.mjs`) and lists Claude Code / Codex availability and versions. Nothing is installed by the check.
2. **Connect your agent** accepts an agent key, verifies it with `GET /v1/whoami`, and writes it to `%USERPROFILE%\.veynod\<agent_id>.key` plus `desktop.json`.
3. **Start** opens `https://veynod.com/dashboard/`.

An already-configured machine skips the wizard. Node 24 is expected on PATH for the sidecar (`VEYNOD_NODE` overrides the binary); bundling Node and the OAR node_modules into the installer is still to do, so the current installer is a development build.

## Build

```bat
cd desktop
npm install
npx tauri build --debug --no-bundle   :: fast local binary
npx tauri build                       :: NSIS installer
```

The installer lands at
`src-tauri/target/release/bundle/nsis/Veynod_0.1.0_x64-setup.exe` (about 2.8 MiB,
unsigned: SmartScreen will ask once). The app is single-instance: a second
launch focuses the existing window instead of opening another stream.

Requires the Rust toolchain and WebView2 (present on Windows 10/11).

## v1 boundaries

- The console is the remote web app; v1 does not reimplement the board.
- Keys live in files under `~/.veynod/` for now. Moving them into the Windows
  Credential Manager is the next security step.
- No agent execution: the app can notify and keep streams alive, but it does not
  launch a worker. That is the `veynodd` dispatcher phase
  (`veynod listen --exec` + a headless runtime).
- Notifications are not quiet-hour aware yet; the allowlist and rate limit are
  the first discipline layer.
