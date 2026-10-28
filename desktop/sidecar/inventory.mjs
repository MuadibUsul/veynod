// Runtime inventory through OAR. One runtime import per module: the root
// entry pulls every adapter and the ACP SDK, which the client must not do.
const RUNTIMES = [
  { id: "claude", spec: "@botiverse/oar/runtimes/claude", exportName: "claudeRuntime" },
  { id: "codex", spec: "@botiverse/oar/runtimes/codex", exportName: "codexRuntime" },
];

export async function inventory() {
  const results = [];
  for (const item of RUNTIMES) {
    try {
      const mod = await import(item.spec);
      const runtime = mod[item.exportName];
      if (!runtime || typeof runtime.installation !== "function") {
        results.push({ id: item.id, available: false, reason: "runtime has no installation probe" });
        continue;
      }
      const snapshot = await runtime.installation();
      if (snapshot?.kind === "available") {
        results.push({
          id: item.id,
          available: true,
          via: snapshot.via,
          command: snapshot.command ?? null,
          version: snapshot.version ?? null,
        });
      } else {
        results.push({
          id: item.id,
          available: false,
          kind: snapshot?.kind ?? "unknown",
          reason: snapshot?.reason ?? null,
        });
      }
    } catch (error) {
      results.push({ id: item.id, available: false, reason: error instanceof Error ? error.message : String(error) });
    }
  }
  return results;
}
