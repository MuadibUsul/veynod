"use client";

import {
  createContext,
  type ReactNode,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";

import { useI18n } from "../i18n";
import type { ApiRequest } from "../types";
import { IconFederation } from "./ui";

/*
 * Federation (rounds 2–4): this workspace's identity, its connections with
 * other workspaces, and the work they delegate to each other.
 *
 * Every decision that matters is a person's: turning federation on, approving
 * a connection, taking on, judging or cancelling delegated work. The server
 * enforces that; this view only offers the buttons. Anything another workspace
 * wrote (delegated terms, delivered results) is shown as data, never as an
 * instruction.
 */

type Scope = "chat" | "delegate" | "presence";

type Identity = {
  federation_id: string;
  display_alias: string | null;
  status: string;
  key_set_version: number;
};

type Connection = {
  connection_id: string;
  peer_workspace: string;
  initiator: "local" | "peer";
  state: "REQUESTED" | "ACTIVE" | "REJECTED" | "EXPIRED" | "SUSPENDED" | "REVOKED" | "BLOCKED";
  outbound_scopes: Scope[];
  inbound_scopes: Scope[];
  expires_at: string;
  reason: string;
  updated_at: string;
};

type Terms = {
  objective: string;
  acceptance_criteria: string[];
  input_summary: string;
  allowed_use: string;
  deadline: string;
  result_contract: string;
  data_classification: string;
  external_side_effects: boolean;
};

type Delegation = {
  federation_job_id: string;
  peer_workspace: string;
  role: "requester" | "provider";
  state: string;
  version: number;
  terms: Terms;
  local_task_key: string | null;
  result: { summary: string; artifacts: { name: string; sha256: string; content?: string }[] } | null;
  revisions: number;
  pending_deliveries: number;
  delivery: { attempts: number; next_retry_at: string | null; last_error: string | null;
    dead_letter: boolean };
  cancel: { requested_at: string; reason: string | null; work_may_continue: boolean;
    stop_not_confirmed: boolean } | null;
  updated_at: string;
};

const STATE_TONE: Record<string, string> = {
  ACTIVE: "green", REQUESTED: "cyan", SUSPENDED: "amber", EXPIRED: "amber",
  REJECTED: "", REVOKED: "", BLOCKED: "red",
  OFFERED: "cyan", ACCEPTED: "cyan", RUNNING: "cyan", SUBMITTED: "amber",
  ACCEPTED_RESULT: "green", CANCELLED: "", DISPUTED: "red", FAILED: "red", OFFERED_TO_US: "amber",
};

const STATE_LABEL: Record<string, string> = {
  ACTIVE: "Connected", REQUESTED: "Waiting for approval", SUSPENDED: "Frozen",
  EXPIRED: "Expired", REJECTED: "Declined", REVOKED: "Ended", BLOCKED: "Blocked",
  OFFERED: "Offered", ACCEPTED: "Taken on", RUNNING: "In progress",
  SUBMITTED: "Delivered, waiting for review", ACCEPTED_RESULT: "Accepted",
  CANCELLED: "Cancelled", DISPUTED: "In dispute", FAILED: "Failed",
  OFFERED_TO_US: "Waiting for you to decide",
};

const SCOPE_LABEL: Record<Scope, string> = {
  chat: "Chat",
  delegate: "Delegate work",
  presence: "See availability",
};

const SCOPES: Scope[] = ["chat", "delegate", "presence"];
const shortId = (id: string) => (id.length > 22 ? `${id.slice(0, 16)}…${id.slice(-4)}` : id);
const PeerNames = createContext<Record<string, string>>({});

function Peer({ id }: { id: string }) {
  const names = useContext(PeerNames);
  return <span title={id}>{names[id] ?? shortId(id)}</span>;
}
const DISABLED = "federation is disabled";

export function FederationView({ request, notify, sessionMode }: {
  request: ApiRequest;
  notify: (message: string) => void;
  /** Federation needs a person's session; the operator token cannot act here. */
  sessionMode: boolean;
}) {
  const { t } = useI18n();
  const [state, setState] = useState<"loading" | "off" | "ready" | "error">("loading");
  const [me, setMe] = useState<Identity | null>(null);
  const [connections, setConnections] = useState<Connection[]>([]);
  const [delegations, setDelegations] = useState<Delegation[]>([]);
  const [busy, setBusy] = useState(false);
  const [names, setNames] = useState<Record<string, string>>({});

  const load = useCallback(async () => {
    try {
      const mine = await request<{ identity: Identity | null }>("/v1/federation/me");
      setMe(mine.identity);
      if (mine.identity) {
        const [conn, dele] = await Promise.all([
          request<{ items: Connection[] }>("/v1/federation/connections"),
          request<{ items: Delegation[] }>("/v1/federation/delegations"),
        ]);
        setConnections(conn.items ?? []);
        setDelegations(dele.items ?? []);
        const peers = new Set([...(conn.items ?? []).map((item) => item.peer_workspace),
          ...(dele.items ?? []).map((item) => item.peer_workspace)]);
        const found = await Promise.all([...peers].map(async (fid) => {
          try {
            const view = await request<{ identity: Identity }>(`/v1/federation/identities/${fid}`);
            return [fid, view.identity.display_alias] as const;
          } catch {
            return [fid, null] as const;
          }
        }));
        // A handle is a convenience for people; the id stays the authority.
        setNames(Object.fromEntries(found.filter(([, alias]) => alias).map(([fid, alias]) => [fid,
          String(alias)])));
      }
      setState("ready");
    } catch (error) {
      setState(String((error as Error)?.message ?? "").includes(DISABLED) ? "off" : "error");
    }
  }, [request]);

  useEffect(() => {
    if (sessionMode) void load();
  }, [load, sessionMode]);

  const call = useCallback(async (path: string, body: unknown, done: string, method = "POST") => {
    setBusy(true);
    try {
      const result = await request<Record<string, unknown>>(path, {
        method,
        body: body === undefined ? undefined : JSON.stringify(body),
      });
      notify(t(done));
      await load();
      return result;
    } catch (error) {
      notify((error as Error)?.message ?? t("Something went wrong."));
      return null;
    } finally {
      setBusy(false);
    }
  }, [request, notify, load, t]);

  if (!sessionMode) {
    return (
      <Empty title={t("Sign in as a person to use federation")}
        text={t("Connections and delegated work are decided by people, so this page needs your own account, not the operator token.")} />
    );
  }
  if (state === "loading") return <div className="skeleton block" />;
  if (state === "off") {
    return (
      <Empty title={t("Federation is turned off on this server")}
        text={t("An operator turns it on for the whole server. Until then nothing here leaves this workspace.")} />
    );
  }
  if (state === "error") {
    return (
      <Empty title={t("Federation could not be loaded")} text={t("Try again in a moment.")}>
        <button type="button" className="button small" onClick={() => void load()}>{t("Retry")}</button>
      </Empty>
    );
  }

  return (
    <div className="federation-view">
      <IdentityCard me={me} busy={busy} call={call} />
      {me && (
        <>
          <PeerNames.Provider value={names}>
            <ConnectionsCard connections={connections} busy={busy} call={call} request={request}
              notify={notify} />
            <DelegationsCard delegations={delegations} connections={connections} busy={busy}
              call={call} />
          </PeerNames.Provider>
        </>
      )}
    </div>
  );
}

type Call = (path: string, body: unknown, done: string, method?: string)
  => Promise<Record<string, unknown> | null>;

function Empty({ title, text, children }: { title: string; text: string; children?: ReactNode }) {
  return (
    <div className="empty-state">
      <div className="empty-mark"><IconFederation size={30} /></div>
      <h2>{title}</h2>
      <p>{text}</p>
      {children}
    </div>
  );
}

function Chip({ value }: { value: string }) {
  const { t } = useI18n();
  return (
    <span className="flow-aside">
      <i className={`lamp ${STATE_TONE[value] ?? ""}`} /> {t(STATE_LABEL[value] ?? value)}
    </span>
  );
}

function IdentityCard({ me, busy, call }: { me: Identity | null; busy: boolean; call: Call }) {
  const { t } = useI18n();
  const [handle, setHandle] = useState("");
  if (!me) {
    return (
      <section className="lin-list" aria-label={t("Federation identity")}>
        <div className="lin-group"><i className="lamp" />{t("Federation identity")}</div>
        <div className="lin-row quiet">
          <span className="lin-title">
            {t("This workspace has no federation identity yet. Only an owner can turn it on.")}
          </span>
          <span className="lin-actions">
            <button type="button" className="button small" disabled={busy}
              onClick={() => void call("/v1/federation/enable", undefined,
                "Federation is on for this workspace.")}>{t("Turn on federation")}</button>
          </span>
        </div>
      </section>
    );
  }
  return (
    <section className="lin-list" aria-label={t("Federation identity")}>
      <div className="lin-group"><i className="lamp green" />{t("Federation identity")}</div>
      <div className="lin-row">
        <span className="lin-title">{t("Federation id")}</span>
        <code className="federation-id">{me.federation_id}</code>
        <span className="lin-actions">
          <button type="button" className="button ghost small"
            onClick={() => void navigator.clipboard?.writeText(me.federation_id)}>{t("Copy")}</button>
        </span>
      </div>
      <form className="lin-row" onSubmit={(event) => {
        event.preventDefault();
        if (handle.trim()) {
          void call("/v1/federation/me/alias", { handle: handle.trim() }, "Handle published.", "PUT");
          setHandle("");
        }
      }}>
        <span className="lin-title">{t("Handle")}</span>
        <span className="flow-aside">{me.display_alias ?? t("none yet")}</span>
        <input value={handle} onChange={(event) => setHandle(event.target.value)} maxLength={64}
          placeholder={t("new handle (owners only)")} aria-label={t("New handle")} />
        <button type="submit" className="button ghost small" disabled={busy || !handle.trim()}>
          {t("Publish")}
        </button>
      </form>
      <div className="lin-row quiet">
        <span className="lin-title">
          {t("Others find you by this id or handle. A handle is never given to anyone else, even after you change it.")}
        </span>
      </div>
    </section>
  );
}

function ScopePicker({ value, onChange, label }: {
  value: Scope[]; onChange: (next: Scope[]) => void; label: string;
}) {
  const { t } = useI18n();
  return (
    <fieldset className="federation-scopes">
      <legend>{label}</legend>
      {SCOPES.map((scope) => (
        <label key={scope} className="check">
          <input type="checkbox" checked={value.includes(scope)}
            onChange={(event) => onChange(event.target.checked
              ? [...value, scope] : value.filter((item) => item !== scope))} />
          {t(SCOPE_LABEL[scope])}
        </label>
      ))}
    </fieldset>
  );
}

function ConnectionsCard({ connections, busy, call, request, notify }: {
  connections: Connection[]; busy: boolean; call: Call; request: ApiRequest;
  notify: (message: string) => void;
}) {
  const { t } = useI18n();
  const [open, setOpen] = useState(false);
  const [peer, setPeer] = useState("");
  const [ours, setOurs] = useState<Scope[]>(["chat"]);
  const [theirs, setTheirs] = useState<Scope[]>(["chat"]);
  const [reason, setReason] = useState("");
  const live = connections.filter((item) => item.state === "ACTIVE").length;

  async function resolvePeer(): Promise<string | null> {
    const value = peer.trim();
    if (value.startsWith("fed:workspace:")) return value;
    try {
      const found = await request<{ status: string; matches: { federation_id: string }[] }>(
        `/v1/federation/resolve?workspace=${encodeURIComponent(value)}`);
      if (found.status === "unique" || found.status === "redirect") return found.matches[0].federation_id;
      notify(found.status === "ambiguous"
        ? t("Several workspaces use that handle; paste the federation id instead.")
        : t("No workspace has that handle."));
    } catch (error) {
      notify((error as Error)?.message ?? t("Something went wrong."));
    }
    return null;
  }

  const act = (item: Connection, action: string, done: string, confirmText?: string) => {
    if (confirmText && !window.confirm(t(confirmText))) return;
    void call(`/v1/federation/connections/${item.connection_id}/${action}`, undefined, done);
  };

  return (
    <section className="lin-list" aria-label={t("Connections")}>
      <div className="lin-group">
        <i className={`lamp ${live ? "green" : ""}`} />{t("Connections")}
        <span className="lin-count">{t("{n} connected", { n: live })}</span>
        <span className="lin-actions">
          <button type="button" className="button ghost small" onClick={() => setOpen(!open)}>
            {open ? t("Close") : t("Connect to a workspace")}
          </button>
        </span>
      </div>
      {open && (
        <form className="deploy-form" onSubmit={async (event) => {
          event.preventDefault();
          const peerId = await resolvePeer();
          if (!peerId) return;
          const result = await call("/v1/federation/connections", {
            peer_workspace: peerId, outbound_scopes: ours, inbound_scopes: theirs,
            reason: reason.trim(),
          }, "Request sent. Their owner has to approve it.");
          if (result) { setOpen(false); setPeer(""); setReason(""); }
        }}>
          <label>{t("Their federation id or handle")}
            <input value={peer} onChange={(event) => setPeer(event.target.value)} required
              placeholder="fed:workspace:… / handle" />
          </label>
          <label>{t("Why (they will see this)")}
            <input value={reason} onChange={(event) => setReason(event.target.value)} required
              minLength={5} maxLength={500} />
          </label>
          <ScopePicker label={t("We may")} value={ours} onChange={setOurs} />
          <ScopePicker label={t("They may")} value={theirs} onChange={setTheirs} />
          <span className="lin-actions">
            <button type="submit" className="button small" disabled={busy}>{t("Ask to connect")}</button>
            <span className="flow-aside">{t("Only an owner can ask. Their owner decides; they may grant less.")}</span>
          </span>
        </form>
      )}
      {connections.length === 0 && (
        <div className="lin-row quiet"><span className="lin-title">{t("No connections yet.")}</span></div>
      )}
      {connections.map((item) => (
        <div key={item.connection_id} className="lin-row">
          <Chip value={item.state} />
          <span className="lin-title"><Peer id={item.peer_workspace} /></span>
          <span className="flow-aside">
            {t("We may")}: {item.outbound_scopes.map((s) => t(SCOPE_LABEL[s])).join(", ") || "—"}
            {" · "}
            {t("They may")}: {item.inbound_scopes.map((s) => t(SCOPE_LABEL[s])).join(", ") || "—"}
            {" · "}{item.reason}
          </span>
          <span className="lin-actions">
            {item.state === "REQUESTED" && item.initiator === "peer" && (
              <>
                <button type="button" className="button small" disabled={busy}
                  onClick={() => act(item, "approve", "Connected.")}>{t("Approve")}</button>
                <button type="button" className="button ghost small" disabled={busy}
                  onClick={() => act(item, "reject", "Request declined.")}>{t("Decline")}</button>
              </>
            )}
            {item.state === "REQUESTED" && item.initiator === "local" && (
              <button type="button" className="button ghost small" disabled={busy}
                onClick={() => act(item, "revoke", "Request withdrawn.")}>{t("Withdraw")}</button>
            )}
            {item.state === "ACTIVE" && (
              <>
                <button type="button" className="button ghost small" disabled={busy}
                  onClick={() => act(item, "freeze", "Connection frozen.")}>{t("Freeze")}</button>
                <button type="button" className="button ghost small" disabled={busy}
                  onClick={() => act(item, "revoke", "Connection ended.",
                    "End this connection? They will be told, and nothing more can pass.")}>{t("End")}</button>
              </>
            )}
            {item.state === "SUSPENDED" && (
              <button type="button" className="button ghost small" disabled={busy}
                onClick={() => act(item, "unfreeze", "Connection unfrozen.")}>{t("Unfreeze")}</button>
            )}
            {["REQUESTED", "ACTIVE", "SUSPENDED", "EXPIRED"].includes(item.state) && (
              <button type="button" className="button ghost small" disabled={busy}
                onClick={() => act(item, "block", "Workspace blocked.",
                  "Block this workspace? They are not told, and their requests will be refused.")}>
                {t("Block")}
              </button>
            )}
          </span>
        </div>
      ))}
    </section>
  );
}

function OfferForm({ connections, busy, call, onDone }: {
  connections: Connection[]; busy: boolean; call: Call; onDone: () => void;
}) {
  const { t } = useI18n();
  const names = useContext(PeerNames);
  const peers = connections.filter(
    (item) => item.state === "ACTIVE" && item.outbound_scopes.includes("delegate"));
  const tomorrow = new Date(Date.now() + 3 * 86400_000).toISOString().slice(0, 10);
  const [peer, setPeer] = useState(peers[0]?.peer_workspace ?? "");
  const [objective, setObjective] = useState("");
  const [criteria, setCriteria] = useState("");
  const [summary, setSummary] = useState("");
  const [allowed, setAllowed] = useState("Only for this task; do not keep the inputs.");
  const [contract, setContract] = useState("");
  const [deadline, setDeadline] = useState(tomorrow);
  const [classification, setClassification] = useState("internal");
  const [effects, setEffects] = useState(false);
  if (peers.length === 0) {
    return (
      <div className="lin-row quiet">
        <span className="lin-title">
          {t("To delegate work you need a connection where we may delegate work.")}
        </span>
      </div>
    );
  }
  return (
    <form className="deploy-form" onSubmit={async (event) => {
      event.preventDefault();
      const result = await call("/v1/federation/delegations", {
        peer_workspace: peer,
        terms: {
          objective: objective.trim(),
          acceptance_criteria: criteria.split("\n").map((line) => line.trim()).filter(Boolean),
          input_summary: summary.trim(),
          data_classification: classification,
          allowed_use: allowed.trim(),
          deadline: new Date(`${deadline}T23:59:00Z`).toISOString(),
          budget_cap: null,
          result_contract: contract.trim(),
          cancellation_policy: "drain",
          external_side_effects: effects,
        },
      }, "Work sent. They decide whether to take it on.");
      if (result) onDone();
    }}>
      <label>{t("To")}
        <select value={peer} onChange={(event) => setPeer(event.target.value)}>
          {peers.map((item) => (
            <option key={item.connection_id} value={item.peer_workspace}>
              {names[item.peer_workspace] ?? shortId(item.peer_workspace)}
            </option>
          ))}
        </select>
      </label>
      <label>{t("Deadline")}
        <input type="date" value={deadline} onChange={(event) => setDeadline(event.target.value)} required />
      </label>
      <label className="federation-wide">{t("What should be done")}
        <textarea value={objective} onChange={(event) => setObjective(event.target.value)} required
          maxLength={4000} rows={2} />
      </label>
      <label className="federation-wide">{t("How we will judge it (one per line)")}
        <textarea value={criteria} onChange={(event) => setCriteria(event.target.value)} required rows={3} />
      </label>
      <label className="federation-wide">{t("What they get (a summary; never secrets or internal ids)")}
        <textarea value={summary} onChange={(event) => setSummary(event.target.value)} required rows={2} />
      </label>
      <label>{t("What they deliver")}
        <input value={contract} onChange={(event) => setContract(event.target.value)} required />
      </label>
      <label>{t("How they may use the inputs")}
        <input value={allowed} onChange={(event) => setAllowed(event.target.value)} required />
      </label>
      <label>{t("Data classification")}
        <select value={classification} onChange={(event) => setClassification(event.target.value)}>
          <option value="public">{t("Public")}</option>
          <option value="internal">{t("Internal")}</option>
          <option value="confidential">{t("Confidential")}</option>
        </select>
      </label>
      <label className="check">
        <input type="checkbox" checked={effects} onChange={(event) => setEffects(event.target.checked)} />
        {t("Has effects outside their workspace")}
      </label>
      <span className="lin-actions">
        <button type="submit" className="button small" disabled={busy}>{t("Send work")}</button>
        <span className="flow-aside">
          {t("The terms are frozen once sent. No payment is involved yet.")}
        </span>
      </span>
    </form>
  );
}

function DelegationRow({ item, busy, call }: { item: Delegation; busy: boolean; call: Call }) {
  const { t } = useI18n();
  const [showSubmit, setShowSubmit] = useState(false);
  const [summary, setSummary] = useState("");
  const [content, setContent] = useState("");
  const base = `/v1/federation/delegations/${item.federation_job_id}`;
  const provider = item.role === "provider";
  const askNote = (label: string) => window.prompt(t(label)) ?? null;
  const cancelAsked = item.cancel !== null;

  return (
    <div className="federation-delegation">
      <div className="lin-row">
        <Chip value={provider && item.state === "OFFERED" ? "OFFERED_TO_US" : item.state} />
        <span className="lin-title">{item.terms.objective}</span>
        <span className="flow-aside">
          {provider ? t("Asked of us by") : t("We asked")} <Peer id={item.peer_workspace} />
          {" · "}{t("due")} {new Date(item.terms.deadline).toLocaleDateString()}
          {item.local_task_key ? ` · ${t("task")} ${item.local_task_key}` : ""}
          {item.revisions ? ` · ${t("{n} revisions", { n: item.revisions })}` : ""}
        </span>
        <span className="lin-actions">
          {provider && item.state === "OFFERED" && !cancelAsked && (
            <>
              <button type="button" className="button small" disabled={busy}
                onClick={() => void call(`${base}/accept`, undefined,
                  "Taken on. A task was added to this board.")}>{t("Take on")}</button>
              <button type="button" className="button ghost small" disabled={busy}
                onClick={() => void call(`${base}/reject`, { note: askNote("Why not? (optional)") },
                  "Declined.")}>{t("Decline")}</button>
            </>
          )}
          {provider && ["ACCEPTED", "RUNNING"].includes(item.state) && (
            <button type="button" className="button small" disabled={busy}
              onClick={() => setShowSubmit(!showSubmit)}>{showSubmit ? t("Close") : t("Deliver")}</button>
          )}
          {provider && cancelAsked && ["OFFERED", "ACCEPTED", "RUNNING"].includes(item.state) && (
            <button type="button" className="button ghost small" disabled={busy}
              onClick={() => void call(`${base}/cancel_ack`, undefined,
                "Cancellation confirmed; our task was cancelled.")}>{t("Confirm cancellation")}</button>
          )}
          {!provider && ["OFFERED", "ACCEPTED", "RUNNING"].includes(item.state) && !cancelAsked && (
            <button type="button" className="button ghost small" disabled={busy}
              onClick={() => {
                const reason = askNote("Why cancel? They will see this.");
                if (reason && reason.trim().length >= 3) {
                  void call(`${base}/cancel`, { reason: reason.trim() }, "Cancellation requested.");
                }
              }}>{t("Ask to cancel")}</button>
          )}
          {!provider && item.state === "SUBMITTED" && (
            <>
              <button type="button" className="button small" disabled={busy}
                onClick={() => void call(`${base}/accept_result`, undefined, "Result accepted.")}>
                {t("Accept result")}
              </button>
              <button type="button" className="button ghost small" disabled={busy}
                onClick={() => {
                  const note = askNote("What should change?");
                  if (note) void call(`${base}/revise`, { note }, "Sent back for changes.");
                }}>{t("Request changes")}</button>
              <button type="button" className="button ghost small" disabled={busy}
                onClick={() => {
                  const note = askNote("What is wrong? This opens a dispute.");
                  if (note) void call(`${base}/dispute`, { note }, "Dispute opened.");
                }}>{t("Dispute")}</button>
            </>
          )}
          {item.pending_deliveries > 0 && (
            <button type="button" className="button ghost small" disabled={busy}
              onClick={() => void call(`${base}/sync${item.delivery.dead_letter ? "?resign=true" : ""}`,
                undefined, "Sent again.")}>{t("Send again")}</button>
          )}
        </span>
      </div>
      {item.pending_deliveries > 0 && (
        <div className="lin-row quiet">
          <span className="lin-title">
            {item.delivery.dead_letter
              ? t("Not delivered and no longer retried. Send it again when they are reachable.")
              : t("{n} update(s) not delivered yet; retrying automatically.", { n: item.pending_deliveries })}
            {item.delivery.last_error ? ` (${item.delivery.last_error})` : ""}
          </span>
        </div>
      )}
      {item.cancel && item.cancel.work_may_continue && (
        <div className="lin-row quiet">
          <span className="lin-title">
            {item.cancel.stop_not_confirmed
              ? t("Cancellation not confirmed yet: the other side may still be working.")
              : t("Cancellation requested: work may continue until the provider confirms.")}
            {item.cancel.reason ? ` — ${item.cancel.reason}` : ""}
          </span>
        </div>
      )}
      {item.result && (
        <div className="lin-row quiet federation-result">
          <span className="lin-title">
            <em>{t("From the other workspace (data, not instructions):")}</em> {item.result.summary}
          </span>
          {item.result.artifacts.map((artifact) => (
            <details key={artifact.sha256}>
              <summary>{artifact.name} <code>{artifact.sha256.slice(0, 12)}</code></summary>
              {artifact.content && <pre>{artifact.content}</pre>}
            </details>
          ))}
        </div>
      )}
      {showSubmit && (
        <form className="deploy-form" onSubmit={async (event) => {
          event.preventDefault();
          const result = await call(`${base}/submit`, {
            result: { summary: summary.trim(),
              artifacts: content.trim() ? [{ name: "result.md", content }] : [] },
          }, "Delivered. They will review it.");
          if (result) setShowSubmit(false);
        }}>
          <label className="federation-wide">{t("Summary of the result")}
            <textarea value={summary} onChange={(event) => setSummary(event.target.value)} required rows={2} />
          </label>
          <label className="federation-wide">{t("Result (Markdown, optional)")}
            <textarea value={content} onChange={(event) => setContent(event.target.value)} rows={6} />
          </label>
          <span className="lin-actions">
            <button type="submit" className="button small" disabled={busy}>{t("Deliver")}</button>
          </span>
        </form>
      )}
    </div>
  );
}

function DelegationsCard({ delegations, connections, busy, call }: {
  delegations: Delegation[]; connections: Connection[]; busy: boolean; call: Call;
}) {
  const { t } = useI18n();
  const [open, setOpen] = useState(false);
  const waiting = useMemo(() => delegations.filter((item) =>
    (item.role === "provider" && item.state === "OFFERED")
    || (item.role === "requester" && item.state === "SUBMITTED")).length, [delegations]);
  return (
    <section className="lin-list" aria-label={t("Delegated work")}>
      <div className="lin-group">
        <i className={`lamp ${waiting ? "amber" : ""}`} />{t("Delegated work")}
        <span className="lin-count">{t("{n} waiting for you", { n: waiting })}</span>
        <span className="lin-actions">
          <button type="button" className="button ghost small" onClick={() => setOpen(!open)}>
            {open ? t("Close") : t("Delegate work")}
          </button>
        </span>
      </div>
      {open && <OfferForm connections={connections} busy={busy} call={call} onDone={() => setOpen(false)} />}
      {delegations.length === 0 && (
        <div className="lin-row quiet"><span className="lin-title">{t("No delegated work yet.")}</span></div>
      )}
      {delegations.map((item) => (
        <DelegationRow key={`${item.peer_workspace}:${item.federation_job_id}`} item={item} busy={busy}
          call={call} />
      ))}
    </section>
  );
}
