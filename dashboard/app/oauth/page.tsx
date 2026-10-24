"use client";

import { useEffect, useState } from "react";

import { LanguageToggle, useI18n } from "../i18n";
import { BrandMark } from "../components/ui";
import { normalizeBase } from "../types";

const SESSION_KEY = "agentgraph.session_token";
const BASE_KEY = "agentgraph.api_base";
const RETURN_KEY = "agentgraph.return_to";

type OAuthView = {
  request_id: string;
  status: "pending" | "approved" | "denied" | "consumed" | "expired";
  client_name: string;
  redirect_host: string;
  scope: string;
  expires_at: string;
  workspace: { id: string; name: string };
  can_approve: boolean;
};

const ROLES = ["EXECUTOR", "ANALYST", "SCOUT", "AUDITOR"] as const;

function defaultAgentName(client: string): string {
  const slug = client.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");
  return (slug || "mcp-client").slice(0, 40);
}

// OAuth consent for MCP clients (claude.ai connectors, Claude Desktop, IDEs):
// the owner approves, Veynod mints an agent key and sends the browser back
// to the client with a one-time code.
export default function OAuthConsentPage() {
  const { t } = useI18n();
  const [apiBase, setApiBase] = useState("");
  const [session, setSession] = useState<string | null>(null);
  const [view, setView] = useState<OAuthView | null>(null);
  const [agent, setAgent] = useState("mcp-client");
  const [role, setRole] = useState<(typeof ROLES)[number]>("EXECUTOR");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [leaving, setLeaving] = useState<string | null>(null);

  useEffect(() => {
    const base =
      localStorage.getItem(BASE_KEY) ||
      (window.location.port === "3000" ? "http://localhost:8000" : window.location.origin);
    setApiBase(base);
    const token = localStorage.getItem(SESSION_KEY);
    if (!token) {
      try {
        sessionStorage.setItem(RETURN_KEY, window.location.pathname + window.location.search);
      } catch {
        // The person signs in and connects again from the client.
      }
      window.location.assign("/dashboard/login/");
      return;
    }
    setSession(token);
    const id = new URLSearchParams(window.location.search).get("request");
    if (!id) {
      setError(t("This link is incomplete. Start the connection again from your client."));
      return;
    }
    void (async () => {
      try {
        const found = (await call(`/v1/auth/oauth/requests/${encodeURIComponent(id)}`, {}, base, token)) as OAuthView;
        setView(found);
        setAgent(defaultAgentName(found.client_name));
      } catch (caught) {
        setError(caught instanceof Error ? caught.message : String(caught));
      }
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function call(path: string, init: RequestInit = {}, base = apiBase, token = session) {
    const response = await fetch(`${normalizeBase(base)}${path}`, {
      ...init,
      headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
      signal: AbortSignal.timeout(20000),
    });
    if (response.status === 401) {
      localStorage.removeItem(SESSION_KEY);
      try {
        sessionStorage.setItem(RETURN_KEY, window.location.pathname + window.location.search);
      } catch {
        // ignore
      }
      window.location.assign("/dashboard/login/");
      throw new Error(t("Your session expired. Sign in again."));
    }
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body?.error?.message ?? t("Something went wrong."));
    return body;
  }

  async function decide(action: "approve" | "deny") {
    if (!view) return;
    setBusy(true);
    setError(null);
    try {
      const body = await call(
        `/v1/auth/oauth/requests/${encodeURIComponent(view.request_id)}/${action}`,
        {
          method: "POST",
          body: action === "approve"
            ? JSON.stringify({ agent_signature: agent.trim() || "mcp-client", role_scope: role })
            : undefined,
        },
      );
      setLeaving(body.redirect_to);
      window.location.assign(body.redirect_to);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
      setBusy(false);
    }
  }

  const closed =
    view && view.status !== "pending"
      ? view.status === "expired"
        ? t("This request expired. Start the connection again from your client.")
        : t("This request was already answered.")
      : null;

  return (
    <div className="auth-page auth-page-connect">
      <div className="auth-aurora" aria-hidden />
      <div className="auth-grid-layer" aria-hidden />
      <header className="auth-header">
        <a className="auth-brand" href="/dashboard/">
          <span className="auth-brand-mark"><BrandMark size={34} /></span>
          <span>
            <strong>Veynod<span className="accent">.</span></strong>
            <small>{t("Human · Agent Blackboard")}</small>
          </span>
        </a>
        <div className="auth-header-actions">
          <LanguageToggle />
        </div>
      </header>

      <main className="auth-connect-main">
        <section className="auth-connect-story">
          <div className="auth-kicker">
            <span className="status-dot live" />
            MCP · OAuth
          </div>
          <h1>{t("Connect an app to your board")}</h1>
          <p>
            {t("An MCP client asked to join this workspace as an agent. Approving gives it its own agent key; you can pause or revoke it any time under Agents.")}
          </p>
          <ul className="auth-connect-list">
            <li>{t("It reads and writes only what its role and namespaces allow.")}</li>
            <li>{t("You are sent back to the app once you decide.")}</li>
            <li>{t("Only approve a connection you just started yourself.")}</li>
          </ul>
        </section>

        <section className="auth-card-wrap">
          <div className="auth-card">
            <div className="auth-card-head">
              <span className="auth-card-kicker">{t("Connection request")}</span>
              <h2>{view ? t("Connect {client}?", { client: view.client_name || "MCP client" }) : t("Loading…")}</h2>
            </div>
            {leaving ? (
              <p className="field-hint">{t("Returning to the app…")}</p>
            ) : (
              <div className="auth-form">
                {view && (
                  <dl className="cli-facts">
                    <div><dt>{t("App")}</dt><dd>{view.client_name || "MCP client"}</dd></div>
                    <div><dt>{t("Returns to")}</dt><dd>{view.redirect_host}</dd></div>
                    <div><dt>{t("Workspace")}</dt><dd>{view.workspace.name}</dd></div>
                  </dl>
                )}
                {error && <div className="inline-error" role="alert">{error}</div>}
                {closed ? (
                  <div className="inline-error" role="alert">{closed}</div>
                ) : view && !view.can_approve ? (
                  <div className="inline-error" role="alert">
                    {t("Only an owner or admin of this workspace can approve. Ask one to open this link.")}
                  </div>
                ) : view ? (
                  <>
                    <label className="field">
                      <span>{t("Agent name")}</span>
                      <input value={agent} onChange={(event) => setAgent(event.target.value)}
                        maxLength={100} spellCheck={false} />
                    </label>
                    <label className="field">
                      <span>{t("Role")}</span>
                      <select value={role} onChange={(event) => setRole(event.target.value as typeof role)}>
                        {ROLES.map((item) => <option key={item} value={item}>{item}</option>)}
                      </select>
                    </label>
                    <button type="button" className="auth-submit" disabled={busy}
                      onClick={() => void decide("approve")}>
                      {busy ? t("Approving…") : t("Approve and connect")}
                      <span aria-hidden>→</span>
                    </button>
                    <button type="button" className="cli-deny" disabled={busy}
                      onClick={() => void decide("deny")}>
                      {t("Refuse")}
                    </button>
                  </>
                ) : null}
              </div>
            )}
          </div>
        </section>
      </main>
    </div>
  );
}
