// The coding agents a computer runs tasks with, driven through their own
// command-line tools: no adapter layer between Veynod and the agent.
//
// Every run gets Veynod's MCP server (and, for Kernel coding work, the
// Kernel's) with the run's own key, and none of the user's own MCP servers,
// so the agent claims as this computer's agent and nobody else:
//   Claude Code  --mcp-config <file> --strict-mcp-config
//   Codex        --ignore-user-config (its login still comes from CODEX_HOME)
//                + -c mcp_servers.<name>.url / .bearer_token_env_var
// Both run without approval prompts: nobody answers one in an unattended run.
// Both sign in the way the person already does, so a subscription works as is:
// on a PC the existing login, on a server `claude setup-token`
// (CLAUDE_CODE_OAUTH_TOKEN) or `codex login --device-auth`.
//
// Flags checked against Claude Code 2.1 and Codex CLI 0.167 (`--help`, and a
// config written by `codex mcp add --url --bearer-token-env-var`).

export const AGENTS = {
  claude: { id: "claude", name: "Claude Code", command: "claude" },
  codex: { id: "codex", name: "Codex", command: "codex" },
};

/** A TOML basic string: JSON's escaping is valid TOML for these values. */
const toml = (value) => JSON.stringify(String(value));

/** The environment variable that carries one MCP server's bearer token to Codex. */
export function tokenVariable(name) {
  return `VEYNOD_MCP_${name.toUpperCase().replace(/[^A-Z0-9]+/g, "_")}_TOKEN`;
}

/**
 * Codex's `-c` overrides for an MCP config in Claude's shape
 * ({mcpServers: {name: {url, headers}}}). A bearer token never goes on the
 * command line, where other processes can read it: Codex reads it from the
 * environment variable named here.
 */
export function codexMcpOverrides(mcp) {
  const args = [];
  const env = {};
  for (const [name, server] of Object.entries(mcp?.mcpServers || {})) {
    if (!/^[A-Za-z0-9_-]+$/.test(name)) throw new Error(`MCP server name ${name} cannot be given to Codex`);
    if (!server?.url) throw new Error(`MCP server ${name} has no url; Codex takes HTTP servers only here`);
    args.push("-c", `mcp_servers.${name}.url=${toml(server.url)}`);
    const auth = server.headers?.Authorization ?? server.headers?.authorization;
    if (auth) {
      const variable = tokenVariable(name);
      env[variable] = String(auth).replace(/^Bearer\s+/i, "");
      args.push("-c", `mcp_servers.${name}.bearer_token_env_var=${toml(variable)}`);
    }
  }
  return { args, env };
}

/**
 * `codex exec` for one unattended turn. With `mcp`, the user's config.toml is
 * left out (its MCP servers would claim with the user's key) and only the
 * given servers are attached; without it the run is as the user configured it.
 * No sandbox and no approvals, as OAR ran Codex (danger-full-access, never):
 * the work needs git, package managers and the network.
 */
export function codexInvocation({ prompt, model = "", mcp = null }) {
  const overrides = mcp ? codexMcpOverrides(mcp) : { args: [], env: {} };
  return {
    command: AGENTS.codex.command,
    args: ["exec", ...(mcp ? ["--ignore-user-config"] : []), "--skip-git-repo-check",
      "--dangerously-bypass-approvals-and-sandbox", ...overrides.args,
      ...(model ? ["--model", model] : []), prompt],
    env: overrides.env,
  };
}

/** `claude -p` for one unattended turn; `mcpFile` holds the servers (strict: nothing else loads). */
export function claudeInvocation({ prompt, model = "", permissionMode = "bypassPermissions", mcpFile = null }) {
  return {
    command: AGENTS.claude.command,
    args: ["-p", prompt, "--permission-mode", permissionMode, ...(model ? ["--model", model] : []),
      ...(mcpFile ? ["--mcp-config", mcpFile, "--strict-mcp-config"] : [])],
    env: {},
  };
}
