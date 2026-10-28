// One real wake through OAR: open a session, deliver the prompt, report where
// it landed and the root status shortly after. The caller owns the session
// lifetime; this probe disposes it so a wizard test does not leave work behind.
const RUNTIMES = {
  claude: { spec: "@botiverse/oar/runtimes/claude", exportName: "claudeRuntime" },
  codex: { spec: "@botiverse/oar/runtimes/codex", exportName: "codexRuntime" },
};

export async function wake({ runtime: runtimeId, cwd, prompt }) {
  const entry = RUNTIMES[runtimeId];
  if (!entry) return { ok: false, reason: "unsupported runtime in this slice" };
  if (!cwd) return { ok: false, reason: "a project folder is required" };
  const text = (prompt || "").trim() || "Reply with the single word OK.";
  const mod = await import(entry.spec);
  const runtime = mod[entry.exportName];
  const installation = await runtime.installation();
  if (installation?.kind !== "available") {
    return { ok: false, reason: "runtime unavailable", kind: installation?.kind ?? null };
  }
  const session = await runtime.session(installation, { cwd });
  try {
    const delivered = await session.deliver(text, { when: "now", origin: "host" });
    await new Promise((resolve) => setTimeout(resolve, 3000));
    const status = session.status?.();
    return {
      ok: delivered.landed !== "rejected",
      session_id: session.id,
      landed: delivered.landed,
      input_id: delivered.inputId ?? null,
      status: status?.value?.kind ?? null,
      reason: delivered.landed === "rejected" ? delivered.reason : null,
    };
  } finally {
    await session.dispose().catch(() => {});
  }
}
