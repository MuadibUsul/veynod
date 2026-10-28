// Quota and model catalogue for one runtime. Never returns account email or
// tokens; only the plan, the windows and the model names the owner can see.
const RUNTIMES = {
  claude: { spec: "@botiverse/oar/runtimes/claude", exportName: "claudeRuntime" },
  codex: { spec: "@botiverse/oar/runtimes/codex", exportName: "codexRuntime" },
};

export async function describe(runtimeId) {
  const entry = RUNTIMES[runtimeId];
  if (!entry) return { id: runtimeId, ok: false, reason: "unsupported runtime in this slice" };
  try {
    const mod = await import(entry.spec);
    const runtime = mod[entry.exportName];
    if (!runtime) return { id: runtimeId, ok: false, reason: "runtime export missing" };
    const installation = await runtime.installation();
    if (installation?.kind !== "available") {
      return {
        id: runtimeId,
        ok: false,
        kind: installation?.kind ?? "unknown",
        reason: installation?.reason ?? null,
      };
    }
    const result = {
      id: runtimeId,
      ok: true,
      installation: {
        via: installation.via,
        command: installation.command ?? null,
        version: installation.version ?? null,
      },
      models: [],
      usage: null,
    };
    if (typeof runtime.listModels === "function") {
      try {
        const models = await runtime.listModels(installation);
        if (models?.kind === "ok") {
          result.models = models.models.map((item) => ({
            id: item.id,
            display_name: item.displayName ?? item.id,
          }));
        }
      } catch (error) {
        result.models_error = error instanceof Error ? error.message : String(error);
      }
    }
    if (typeof runtime.accountUsage === "function") {
      try {
        const usage = await runtime.accountUsage(installation);
        if (usage?.kind === "available") {
          result.usage = {
            plan: usage.plan ?? null,
            rate_limited: Boolean(usage.rateLimited),
            windows: (usage.windows ?? []).map((item) => ({
              label: item.label,
              used_ratio: item.usedRatio,
              resets_at: item.resetsAt ?? null,
            })),
          };
        }
      } catch (error) {
        result.usage_error = error instanceof Error ? error.message : String(error);
      }
    }
    return result;
  } catch (error) {
    return { id: runtimeId, ok: false, reason: error instanceof Error ? error.message : String(error) };
  }
}
