// The model providers a computer's agent can run on.
//
// `npx veynod --provider deepseek --model deepseek-chat` saves the choice; the
// runner then starts the agent pointed at that provider. The key never passes
// through Veynod: it is read on the computer from the environment variable
// named here (`key`), at the moment a task starts.
//
// Two ways in, by what the provider speaks:
//   anthropic: Claude Code with ANTHROPIC_BASE_URL / ANTHROPIC_AUTH_TOKEN;
//   openai:    Codex with a model provider of its own (`-c model_providers...`).
// `native` is the agent's own sign-in: nothing is changed but the model name.

export const PROVIDERS = [
  { id: "claude", name: "Claude", runtime: "claude", native: true },
  { id: "openai", name: "OpenAI", runtime: "codex", native: true },
  { id: "deepseek", name: "DeepSeek", runtime: "claude", anthropic: "https://api.deepseek.com/anthropic",
    key: "DEEPSEEK_API_KEY" },
  { id: "kimi", name: "Kimi", runtime: "claude", anthropic: "https://api.moonshot.ai/anthropic",
    key: "MOONSHOT_API_KEY" },
  { id: "glm", name: "GLM", runtime: "claude", anthropic: "https://api.z.ai/api/anthropic", key: "ZAI_API_KEY" },
  { id: "qwen", name: "Qwen", runtime: "claude", anthropic: "https://dashscope-intl.aliyuncs.com/apps/anthropic",
    key: "DASHSCOPE_API_KEY" },
  { id: "minimax", name: "MiniMax", runtime: "claude", anthropic: "https://api.minimax.io/anthropic",
    key: "MINIMAX_API_KEY" },
  { id: "openrouter", name: "OpenRouter", runtime: "claude", anthropic: "https://openrouter.ai/api",
    key: "OPENROUTER_API_KEY" },
  { id: "aihubmix", name: "AiHubMix", runtime: "claude", anthropic: "https://aihubmix.com", key: "AIHUBMIX_API_KEY" },
  { id: "gemini", name: "Gemini", runtime: "codex",
    openai: "https://generativelanguage.googleapis.com/v1beta/openai", key: "GEMINI_API_KEY" },
  { id: "mistral", name: "Mistral", runtime: "codex", openai: "https://api.mistral.ai/v1", key: "MISTRAL_API_KEY" },
  { id: "xai", name: "xAI", runtime: "codex", openai: "https://api.x.ai/v1", key: "XAI_API_KEY" },
  { id: "groq", name: "Groq", runtime: "codex", openai: "https://api.groq.com/openai/v1", key: "GROQ_API_KEY" },
];

export const PROVIDER_IDS = PROVIDERS.map((item) => item.id);
export const provider = (id) => PROVIDERS.find((item) => item.id === id) || null;

/** A base URL the owner set for this provider on the computer wins over the preset. */
const baseUrl = (item, env) => env[`VEYNOD_${item.id.toUpperCase()}_BASE_URL`] || item.anthropic || item.openai;

/**
 * What a run on `providerId` adds to the agent's start: {env, args, missing}.
 * `missing` names the environment variable that holds the key when it is not
 * set on this computer; the run is then not started with a half-made setup.
 */
export function providerRun(providerId, { runtime, model = "", env = process.env } = {}) {
  const item = provider(providerId);
  if (!item) throw new Error(`unknown provider ${providerId} (${PROVIDER_IDS.join(", ")})`);
  if (item.native) return { env: {}, args: [] };
  if (item.runtime !== runtime) {
    throw new Error(`${item.name} runs through ${item.runtime === "claude" ? "Claude Code" : "Codex"}, not ${runtime}`);
  }
  const secret = env[item.key];
  if (!secret) return { env: {}, args: [], missing: item.key };
  if (item.anthropic) {
    return { args: [], env: { ANTHROPIC_BASE_URL: baseUrl(item, env), ANTHROPIC_AUTH_TOKEN: secret,
      ...(model ? { ANTHROPIC_MODEL: model, ANTHROPIC_SMALL_FAST_MODEL: model } : {}) } };
  }
  // Codex reads the key itself from the variable it is told about.
  const quoted = (value) => JSON.stringify(String(value));
  return { env: {}, args: [
    "-c", `model_providers.${item.id}.name=${quoted(item.name)}`,
    "-c", `model_providers.${item.id}.base_url=${quoted(baseUrl(item, env))}`,
    "-c", `model_providers.${item.id}.env_key=${quoted(item.key)}`,
    "-c", `model_provider=${quoted(item.id)}`] };
}
