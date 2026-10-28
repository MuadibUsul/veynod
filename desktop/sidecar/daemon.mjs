// Resident wake queue. Sessions stay open, so an idle agent gets a new turn
// now and a busy one gets the prompt after its current turn. A failed local
// wake is marked fallback after three attempts; the caller can then use the
// server inbox/assignment path.
import { claudeRuntime } from "@botiverse/oar/runtimes/claude";
import { codexRuntime } from "@botiverse/oar/runtimes/codex";
import { add, pending, receipt, update } from "./queue.mjs";

const RUNTIMES = { claude: claudeRuntime, codex: codexRuntime };
const sessions = new Map();
const inflight = new Set();

async function sessionFor(item) {
  const key = `${item.runtime}:${item.cwd}`;
  if (sessions.has(key)) return sessions.get(key);
  const runtime = RUNTIMES[item.runtime];
  if (!runtime) throw new Error(`unsupported runtime ${item.runtime}`);
  const installation = await runtime.installation();
  if (installation?.kind !== "available") throw new Error("runtime unavailable");
  const session = await runtime.session(installation, { cwd: item.cwd });
  sessions.set(key, session);
  return session;
}

async function process(item) {
  if (inflight.has(item.input_id)) return;
  inflight.add(item.input_id);
  try {
    const session = await sessionFor(item);
    const status = session.status?.()?.value?.kind ?? "unknown";
    const when = item.urgent ? "interrupt" : status === "running" ? "after_turn" : "now";
    const delivered = await session.deliver(item.prompt, {
      when,
      ...(item.urgent ? { maxWaitMs: 5000 } : {}),
      origin: "host",
      inputId: item.input_id,
    });
    if (delivered.landed === "rejected") throw new Error(delivered.reason || "delivery rejected");
    update(item.input_id, {
      state: delivered.landed === "queued" ? "queued" : "delivered",
      landed: delivered.landed,
      session_id: session.id,
      status_before: status,
      delivered_at: new Date().toISOString(),
    });
    receipt({ input_id: item.input_id, task_key: item.task_key, runtime: item.runtime,
      urgent: Boolean(item.urgent), landed: delivered.landed, session_id: session.id,
      status_before: status });
  } catch (error) {
    const attempts = Number(item.attempts || 0) + 1;
    const failed = attempts >= 3;
    update(item.input_id, {
      attempts,
      state: failed ? "fallback" : "pending",
      error: error instanceof Error ? error.message : String(error),
      ...(failed ? { fallback_at: new Date().toISOString() } : {}),
    });
    receipt({ input_id: item.input_id, task_key: item.task_key, runtime: item.runtime,
      urgent: Boolean(item.urgent), failed, attempts,
      error: error instanceof Error ? error.message : String(error) });
  } finally {
    inflight.delete(item.input_id);
  }
}

async function loop() {
  const items = pending();
  await Promise.all(items.map((item) => process(item)));
  setTimeout(loop, 1000);
}

loop();
