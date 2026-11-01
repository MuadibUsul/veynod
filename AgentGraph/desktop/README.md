# Veynod Desktop (Windows)

> **Frozen (owner decision D-1, 2026-10-30).** This client is not developed
> further and is not part of the supported setup. Every operating system,
> Windows included, connects with the CLI (`npx veynod up` and the hub; see
> [docs/WAKE_AND_DISPATCH.md](../docs/WAKE_AND_DISPATCH.md) §14). The code
> stays here unchanged. If a tray app returns, it will be a view over the
> hub's local socket, with no stream or wake logic of its own.

One client for the Veynod console and the local agent loop. The console is the
web app; the client adds the tray, notifications, a local wake queue and the
first-run setup. The board remains the source of truth.

## Windows

- **Main window**: the deployed console (`https://veynod.com/dashboard/`, or
  the server set in Setup). The diagram is an infinite canvas and owns its own
  wheel/pan/zoom; the client only hides the window scrollbar.
- **Client home** (tray → Client home): what this computer knows — OS/arch,
  client and server version, runtime and project, auto-wake, agents, the wake
  queue with three-stage receipts, guard audit, and open tasks.
- **Inbox & approvals** (tray → Inbox & approvals): unread directed events,
  mark-all-read, and REVIEWING tasks with per-acceptance checkboxes and
  approve/reject (the key needs `task:review`; otherwise the server's 403 is
  shown).
- **Setup & connection**: check local Claude Code / Codex through OAR, read
  quota/models, test a wake, connect an agent key, choose the server (staging
  or local console), and choose the project folder. Auto-wake on a live
  `task.assigned` is on by default.
- **Tray**: Open Veynod / Inbox & approvals / Client home / Setup &
  connection / Full screen / Reload console / Quit. Closing a window hides it;
  use Quit to exit. Do not force-kill the process: Windows leaves the tray icon
  behind until Explorer is restarted.

## Runtime and wake layer

The bundled Node 24 sidecar (`sidecar/`) keeps one OAR session per
runtime+project. A wake is a prompt with a task key and an idempotency key:
idle runs now, a busy session gets it after the turn, urgent interrupts only
after the server lease/holder/fencing check and writes a progress snapshot to
`~/.veynod/wake-audit.jsonl`. Three failures create a server task assigned to
the agent instead of dropping the prompt. Receipts are `delivered`, `accepted`
and `started` in `~/.veynod/wake-receipts.jsonl`; the queue is
`~/.veynod/wake-queue.json`. One daemon per computer (`wake-daemon.lock`).

## Configuration

`%USERPROFILE%\.veynod\desktop.json`:

```json
{
  "agents": ["zcode"],
  "host": "https://veynod.com",
  "runtime": "codex",
  "cwd": "C:\\work\\project",
  "auto_wake": true
}
```

Each agent key is read from `%USERPROFILE%\.veynod\<agent>.key`. The setup
wizard writes both files; the same values can be changed from Setup.

## Build

```bat
cd desktop
npm install
cd sidecar && npm install && cd ..
npm run build
```

`npm run build` copies the bundled Node 24 runtime into the sidecar and
builds the NSIS installer at
`src-tauri/target/release/bundle/nsis/Veynod_0.1.0_x64-setup.exe` (~39 MiB,
unsigned: SmartScreen asks once). Every sidecar module and the design-token
stylesheet must be present in the frontend/resources; the installer is
single-instance and creates one tray icon.
