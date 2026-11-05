"use client";

import {
  createContext,
  type ReactNode,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import { useI18n } from "../i18n";
import type { ApiRequest } from "../types";
import { DirectoryCard } from "./federation-directory";
import { MarketCard } from "./federation-market";
import { ReputationPanel } from "./federation-reputation";
import { SkillsCard } from "./federation-skills";
import { IconFederation } from "./ui";

/*
 * Federation: this workspace's identity, its connections with other
 * workspaces, chat with them, and the work they delegate to each other.
 * Four areas (F-05): discover (who to reach), contact (connections and
 * requests), collaborate (chat and delegated work), safety (what may cross,
 * what did not arrive, freeze and block).
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

type Thread = {
  thread_id: string;
  connection_id: string;
  peer_workspace: string;
  last_message_at: string | null;
  messages: number;
  unhandled: number;
};

type Message = {
  message_id: string;
  cursor: string;
  direction: "in" | "out";
  thread_id: string;
  peer_workspace: string;
  state: string;
  reject_code: string | null;
  created_at: string;
  content?: string | null;
  untrusted_content?: string | null;
  delivery?: { attempts: number; last_error?: string | null; next_retry_at?: string | null };
};

type Zone = "discover" | "contact" | "collaborate" | "skills" | "safety";
const ZONES: { key: Zone; label: string }[] = [
  { key: "discover", label: "Discover" },
  { key: "contact", label: "Contact" },
  { key: "collaborate", label: "Collaborate" },
  { key: "skills", label: "Skills" },
  { key: "safety", label: "Safety" },
];

// What a person reads for each message state (ICD_I_MSG §4).
const OUT_LABEL: Record<string, string> = {
  accepted_locally: "Sending", sent: "Sent", received: "Delivered", processed: "Read and handled",
  rejected: "Refused", expired: "Expired", dead_letter: "Not delivered",
};
const IN_LABEL: Record<string, string> = {
  received: "New", processed: "Handled", rejected: "Declined", expired: "Expired",
};
const MESSAGE_TONE: Record<string, string> = {
  accepted_locally: "cyan", sent: "cyan", received: "green", processed: "green",
  rejected: "red", expired: "amber", dead_letter: "red",
};

async function readAll(request: ApiRequest, query: string): Promise<Message[]> {
  const items: Message[] = [];
  let after = "";
  for (let page = 0; page < 10; page += 1) {
    const result = await request<{ items: Message[]; next_cursor: string | null; has_more: boolean }>(
      `/v1/federation/messages?limit=200${query}${after ? `&after=${after}` : ""}`);
    items.push(...(result.items ?? []));
    if (!result.has_more || !result.next_cursor) break;
    after = result.next_cursor;
  }
  return items;
}

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
  budget?: { unit: string; cap: number; spent: number; remaining: number; over: boolean;
    mode?: "enforce" | "shadow"; blocking?: boolean } | null;
  usage?: Record<string, { total: number; measured: number; self_reported: number }>;
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
  const [threads, setThreads] = useState<Thread[]>([]);
  const [zone, setZone] = useState<Zone | null>(null);
  const [connectTo, setConnectTo] = useState("");
  const [busy, setBusy] = useState(false);
  const [names, setNames] = useState<Record<string, string>>({});

  const load = useCallback(async () => {
    try {
      const mine = await request<{ identity: Identity | null }>("/v1/federation/me");
      setMe(mine.identity);
      if (mine.identity) {
        const [conn, dele, thr] = await Promise.all([
          request<{ items: Connection[] }>("/v1/federation/connections"),
          request<{ items: Delegation[] }>("/v1/federation/delegations"),
          request<{ items: Thread[] }>("/v1/federation/threads"),
        ]);
        setConnections(conn.items ?? []);
        setDelegations(dele.items ?? []);
        setThreads(thr.items ?? []);
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

  const pendingRequests = connections.filter((item) =>
    item.state === "REQUESTED" && item.initiator === "peer").length;
  const waitingWork = delegations.filter((item) =>
    (item.role === "provider" && item.state === "OFFERED")
    || (item.role === "requester" && item.state === "SUBMITTED")).length;
  const unhandled = threads.reduce((sum, item) => sum + item.unhandled, 0);
  const restricted = connections.filter((item) =>
    ["SUSPENDED", "BLOCKED", "EXPIRED"].includes(item.state)).length;
  const live = connections.some((item) => item.state === "ACTIVE");
  // Open where something is waiting; otherwise where the work is.
  const current: Zone = zone ?? (pendingRequests ? "contact" : live ? "collaborate" : "discover");
  const badge: Record<Zone, number> = {
    discover: 0, contact: pendingRequests, collaborate: unhandled + waitingWork, skills: 0,
    safety: restricted,
  };

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
        <button type="button" className="button primary small" onClick={() => void load()}>{t("Retry")}</button>
      </Empty>
    );
  }

  return (
    <div className="federation-view">
      {!me && <IdentityCard me={me} busy={busy} call={call} />}
      {me && (
        <PeerNames.Provider value={names}>
          <div className="segmented federation-zones" role="tablist" aria-label={t("Federation areas")}>
            {ZONES.map((item) => (
              <button key={item.key} type="button" role="tab" aria-selected={current === item.key}
                className={current === item.key ? "active" : ""} onClick={() => setZone(item.key)}>
                {t(item.label)}
                {badge[item.key] > 0 && <span className="tab-count">{badge[item.key]}</span>}
              </button>
            ))}
          </div>
          {current === "discover" && (
            <>
              <IdentityCard me={me} busy={busy} call={call} />
              <DiscoverCard request={request} notify={notify} connections={connections}
                onConnect={(fid) => { setConnectTo(fid); setZone("contact"); }} />
              <DirectoryCard request={request} notify={notify}
                onConnect={(fid) => { setConnectTo(fid); setZone("contact"); }} />
            </>
          )}
          {current === "contact" && (
            <ConnectionsCard connections={connections} busy={busy} call={call} request={request}
              notify={notify} prefill={connectTo} onPrefillUsed={() => setConnectTo("")} />
          )}
          {current === "collaborate" && (
            <>
              <ChatCard threads={threads} connections={connections} request={request}
                notify={notify} reload={load} />
              <DelegationsCard delegations={delegations} connections={connections} busy={busy}
                call={call} />
              <MarketCard request={request} notify={notify} myFid={me.federation_id} />
            </>
          )}
          {current === "skills" && <SkillsCard request={request} notify={notify} />}
          {current === "safety" && (
            <SafetyCard connections={connections} delegations={delegations} request={request}
              call={call} busy={busy} />
          )}
        </PeerNames.Provider>
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
            <button type="button" className="button primary small" disabled={busy}
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

function ConnectionsCard({ connections, busy, call, request, notify, prefill, onPrefillUsed }: {
  connections: Connection[]; busy: boolean; call: Call; request: ApiRequest;
  notify: (message: string) => void; prefill?: string; onPrefillUsed?: () => void;
}) {
  const { t } = useI18n();
  const [open, setOpen] = useState(Boolean(prefill));
  const [peer, setPeer] = useState(prefill ?? "");
  useEffect(() => {
    if (prefill) onPrefillUsed?.();
  }, [prefill, onPrefillUsed]);
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
            <button type="submit" className="button primary small" disabled={busy}>{t("Ask to connect")}</button>
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
                <button type="button" className="button primary small" disabled={busy}
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
  const [policy, setPolicy] = useState("drain");
  const [tokenCap, setTokenCap] = useState("");
  const [capMode, setCapMode] = useState<"enforce" | "shadow">("enforce");
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
          budget_cap: tokenCap.trim()
            ? { amount: Number.parseInt(tokenCap, 10), unit: "model_tokens", mode: capMode }
            : null,
          result_contract: contract.trim(),
          cancellation_policy: policy,
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
      <label>{t("If we cancel")}
        <select value={policy} onChange={(event) => setPolicy(event.target.value)}>
          <option value="drain">{t("They may still deliver what is done")}</option>
          <option value="hard_cancel">{t("They must stop and deliver nothing more")}</option>
        </select>
      </label>
      <label>{t("Model token cap (optional, not money)")}
        <input type="number" min={1} step={1} value={tokenCap}
          onChange={(event) => setTokenCap(event.target.value)} />
      </label>
      <label>{t("When the cap is reached")}
        <select value={capMode} disabled={!tokenCap.trim()}
          onChange={(event) => setCapMode(event.target.value as "enforce" | "shadow")}>
          <option value="enforce">{t("Stop: no new run may start")}</option>
          <option value="shadow">{t("Only warn: record it, never block")}</option>
        </select>
      </label>
      <span className="lin-actions">
        <button type="submit" className="button primary small" disabled={busy}>{t("Send work")}</button>
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
          {item.budget
            ? ` · ${t("used {spent} of {cap} {unit}", { spent: item.budget.spent,
              cap: item.budget.cap, unit: item.budget.unit })}${item.budget.over
              ? ` (${item.budget.mode === "shadow" ? t("over the cap: warning only")
                : t("cap reached: no new execution")})` : ""}`
            : ""}
          {Object.entries(item.usage ?? {}).map(([unit, used]) => (
            ` · ${t("{unit}: {measured} measured, {self} self-reported", {
              unit, measured: used.measured, self: used.self_reported })}`
          ))}
        </span>
        <span className="lin-actions">
          {provider && item.state === "OFFERED" && !cancelAsked && (
            <>
              <button type="button" className="button primary small" disabled={busy}
                onClick={() => void call(`${base}/accept`, undefined,
                  "Taken on. A task was added to this board.")}>{t("Take on")}</button>
              <button type="button" className="button ghost small" disabled={busy}
                onClick={() => void call(`${base}/reject`, { note: askNote("Why not? (optional)") },
                  "Declined.")}>{t("Decline")}</button>
            </>
          )}
          {provider && ["ACCEPTED", "RUNNING"].includes(item.state) && (
            <button type="button" className="button primary small" disabled={busy}
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
              <button type="button" className="button primary small" disabled={busy}
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
            <button type="submit" className="button primary small" disabled={busy}>{t("Deliver")}</button>
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

function DiscoverCard({ request, notify, connections, onConnect }: {
  request: ApiRequest; notify: (message: string) => void; connections: Connection[];
  onConnect: (fid: string) => void;
}) {
  const { t } = useI18n();
  const [query, setQuery] = useState("");
  const [found, setFound] = useState<Identity | null>(null);
  const [looking, setLooking] = useState(false);

  async function look() {
    const value = query.trim();
    if (!value) return;
    setLooking(true);
    setFound(null);
    try {
      let fid = value;
      if (!value.startsWith("fed:")) {
        const match = await request<{ status: string; matches: { federation_id: string }[] }>(
          `/v1/federation/resolve?workspace=${encodeURIComponent(value)}`);
        if (match.status !== "unique" && match.status !== "redirect") {
          notify(match.status === "ambiguous"
            ? t("Several workspaces use that handle; paste the federation id instead.")
            : t("No workspace has that handle."));
          return;
        }
        fid = match.matches[0].federation_id;
      }
      const view = await request<{ identity: Identity }>(`/v1/federation/identities/${fid}`);
      setFound(view.identity);
    } catch (error) {
      notify((error as Error)?.message ?? t("Something went wrong."));
    } finally {
      setLooking(false);
    }
  }

  const existing = found && connections.find((item) => item.peer_workspace === found.federation_id);
  return (
    <section className="lin-list" aria-label={t("Find a workspace")}>
      <div className="lin-group"><i className="lamp cyan" />{t("Find a workspace")}</div>
      <form className="lin-row" onSubmit={(event) => { event.preventDefault(); void look(); }}>
        <span className="lin-title">{t("Handle or federation id")}</span>
        <input value={query} onChange={(event) => setQuery(event.target.value)}
          placeholder="handle / fed:workspace:…" aria-label={t("Handle or federation id")} />
        <button type="submit" className="button ghost small" disabled={looking || !query.trim()}>
          {t("Look up")}
        </button>
      </form>
      {found && (
        <div className="lin-row">
          <Chip value={found.status === "active" ? "ACTIVE" : "SUSPENDED"} />
          <span className="lin-title">{found.display_alias ?? shortId(found.federation_id)}</span>
          <code className="federation-id">{found.federation_id}</code>
          <span className="lin-actions">
            {existing ? (
              <span className="flow-aside">{t(STATE_LABEL[existing.state] ?? existing.state)}</span>
            ) : (
              <button type="button" className="button primary small"
                onClick={() => onConnect(found.federation_id)}>{t("Ask to connect")}</button>
            )}
          </span>
        </div>
      )}
      {found && <ReputationPanel fid={found.federation_id} request={request} />}
      <div className="lin-row quiet">
        <span className="lin-title">
          {t("Ask the other team for their handle or federation id. Nothing is shared until both owners agree.")}
        </span>
      </div>
    </section>
  );
}

function ChatCard({ threads, connections, request, notify, reload }: {
  threads: Thread[]; connections: Connection[]; request: ApiRequest;
  notify: (message: string) => void; reload: () => Promise<void>;
}) {
  const { t } = useI18n();
  const names = useContext(PeerNames);
  const label = (fid: string) => names[fid] ?? shortId(fid);
  const chatPeers = connections.filter((item) =>
    item.state === "ACTIVE" && item.outbound_scopes.includes("chat"));
  const [selected, setSelected] = useState<{ thread?: string; peer: string } | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [text, setText] = useState("");
  const [sending, setSending] = useState(false);
  // One key per draft: a double click or a retry never sends the text twice.
  const draftKey = useRef(newKey());

  const active = selected ?? (threads[0]
    ? { thread: threads[0].thread_id, peer: threads[0].peer_workspace } : null);

  const refresh = useCallback(async () => {
    if (!active?.thread) { setMessages([]); return; }
    try {
      setMessages(await readAll(request, `&thread_id=${active.thread}`));
    } catch (error) {
      notify((error as Error)?.message ?? t("Something went wrong."));
    }
  }, [active?.thread, request, notify, t]);

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => {
      if (document.visibilityState === "visible") void refresh();
    }, 8000);
    return () => window.clearInterval(timer);
  }, [refresh]);

  async function post(path: string, body: unknown, done: string) {
    try {
      const result = await request<Message>(path, {
        method: "POST", body: body === undefined ? undefined : JSON.stringify(body),
      });
      notify(t(done));
      await Promise.all([refresh(), reload()]);
      return result;
    } catch (error) {
      notify((error as Error)?.message ?? t("Something went wrong."));
      return null;
    }
  }

  async function send() {
    if (!active || !text.trim()) return;
    setSending(true);
    const sent = await post("/v1/federation/messages", {
      peer_workspace: active.peer, text: text.trim(), idempotency_key: draftKey.current,
      ...(active.thread ? { thread_id: active.thread } : {}),
    }, "Message sent.");
    setSending(false);
    if (sent) {
      setText("");
      draftKey.current = newKey();
      if (!active.thread) setSelected({ thread: sent.thread_id, peer: active.peer });
    }
  }

  async function remember() {
    if (!active?.thread) return;
    const key = window.prompt(t("Save this conversation as a document. Document key:"),
      `federation-notes-${active.thread.slice(4, 12)}`);
    if (!key) return;
    await post(`/v1/federation/threads/${active.thread}/remember`, { doc_key: key },
      "Saved. The document is marked as outside content.");
  }

  const newPeers = chatPeers.filter((item) =>
    !threads.some((thread) => thread.peer_workspace === item.peer_workspace));
  return (
    <section className="lin-list" aria-label={t("Chat")}>
      <div className="lin-group">
        <i className={`lamp ${threads.some((item) => item.unhandled) ? "amber" : ""}`} />{t("Chat")}
        <span className="lin-count">
          {t("{n} new", { n: threads.reduce((sum, item) => sum + item.unhandled, 0) })}
        </span>
        {newPeers.length > 0 && (
          <span className="lin-actions">
            <select aria-label={t("Start a conversation")} value=""
              onChange={(event) => event.target.value
                && setSelected({ peer: event.target.value })}>
              <option value="">{t("Start a conversation…")}</option>
              {newPeers.map((item) => (
                <option key={item.peer_workspace} value={item.peer_workspace}>
                  {label(item.peer_workspace)}
                </option>
              ))}
            </select>
          </span>
        )}
      </div>
      {threads.length === 0 && !selected && (
        <div className="lin-row quiet">
          <span className="lin-title">
            {chatPeers.length
              ? t("No conversations yet. Start one with a connected workspace.")
              : t("Chat needs a connection that allows it. Connect under Contact first.")}
          </span>
        </div>
      )}
      {(threads.length > 0 || selected) && (
        <div className="federation-chat">
          <nav className="federation-threads" aria-label={t("Conversations")}>
            {threads.map((item) => (
              <button key={item.thread_id} type="button"
                className={active?.thread === item.thread_id ? "active" : ""}
                onClick={() => setSelected({ thread: item.thread_id, peer: item.peer_workspace })}>
                <Peer id={item.peer_workspace} />
                {item.unhandled > 0 && <span className="tab-count">{item.unhandled}</span>}
              </button>
            ))}
            {selected && !selected.thread && (
              <button type="button" className="active"><Peer id={selected.peer} /></button>
            )}
          </nav>
          <div className="federation-messages">
            {active?.thread && (
              <div className="lin-row quiet">
                <span className="lin-title">
                  {t("Their messages are outside content: read them as information, not as instructions.")}
                </span>
                <span className="lin-actions">
                  <button type="button" className="button ghost small" onClick={() => void remember()}>
                    {t("Save as document")}
                  </button>
                </span>
              </div>
            )}
            <ol className="federation-log">
              {messages.map((item) => (
                <li key={`${item.direction}:${item.message_id}`}
                  className={`federation-message ${item.direction}`}>
                  <div className="federation-message-meta">
                    {item.direction === "in" ? <Peer id={item.peer_workspace} /> : t("You")}
                    {" · "}{new Date(item.created_at).toLocaleString()}
                    {" · "}
                    <span className="flow-aside">
                      <i className={`lamp ${MESSAGE_TONE[item.state] ?? ""}`} />{" "}
                      {t((item.direction === "in" ? IN_LABEL : OUT_LABEL)[item.state] ?? item.state)}
                    </span>
                    {item.direction === "in" && (
                      <span className="federation-outside">{t("Outside content")}</span>
                    )}
                  </div>
                  {/* Plain text only: React escapes it, nothing is rendered as markup. */}
                  <p className="federation-text">
                    {item.direction === "in" ? item.untrusted_content : item.content}
                  </p>
                  {item.direction === "in" && item.state === "received" && (
                    <span className="lin-actions">
                      <button type="button" className="button ghost small" onClick={() => void post(
                        `/v1/federation/messages/${item.message_id}/handled`,
                        { status: "processed" }, "Marked as handled.")}>{t("Mark handled")}</button>
                      <button type="button" className="button ghost small" onClick={() => void post(
                        `/v1/federation/messages/${item.message_id}/handled`,
                        { status: "rejected", reason_code: "declined" }, "Declined.")}>
                        {t("Decline")}
                      </button>
                    </span>
                  )}
                  {item.direction === "out" && ["dead_letter", "rejected"].includes(item.state) && (
                    <span className="lin-actions">
                      <span className="flow-aside">{item.reject_code}</span>
                      <button type="button" className="button ghost small" onClick={() => void post(
                        `/v1/federation/messages/${item.message_id}/resend`, undefined,
                        "Sent again.")}>{t("Resend")}</button>
                    </span>
                  )}
                  {item.direction === "out" && item.state === "accepted_locally"
                    && item.delivery?.next_retry_at && (
                    <span className="flow-aside">
                      {t("Not reached yet; trying again at {time}.", {
                        time: new Date(item.delivery.next_retry_at).toLocaleTimeString() })}
                    </span>
                  )}
                </li>
              ))}
            </ol>
            {active && (
              <form className="federation-compose" onSubmit={(event) => {
                event.preventDefault();
                void send();
              }}>
                <textarea value={text} onChange={(event) => setText(event.target.value)}
                  maxLength={8000} rows={3} aria-label={t("Message")}
                  placeholder={t("Write to {peer}…", { peer: label(active.peer) })}
                  onKeyDown={(event) => {
                    if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) void send();
                  }} />
                <button type="submit" className="button primary small" disabled={sending || !text.trim()}>
                  {t("Send")}
                </button>
              </form>
            )}
          </div>
        </div>
      )}
    </section>
  );
}

function newKey(): string {
  return typeof crypto !== "undefined" && "randomUUID" in crypto
    ? `ui-${crypto.randomUUID()}` : `ui-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

function SafetyCard({ connections, delegations, request, call, busy }: {
  connections: Connection[]; delegations: Delegation[]; request: ApiRequest; call: Call;
  busy: boolean;
}) {
  const { t } = useI18n();
  const [undelivered, setUndelivered] = useState<Message[] | null>(null);
  useEffect(() => {
    void readAll(request, "&direction=out")
      .then((items) => setUndelivered(items.filter((item) =>
        ["dead_letter", "rejected"].includes(item.state))))
      .catch(() => setUndelivered([]));
  }, [request]);
  const stuck = delegations.filter((item) => item.delivery?.dead_letter);
  const act = (item: Connection, action: string, done: string, confirmText?: string) => {
    if (confirmText && !window.confirm(t(confirmText))) return;
    void call(`/v1/federation/connections/${item.connection_id}/${action}`, undefined, done);
  };
  return (
    <>
      <section className="lin-list" aria-label={t("What may cross")}>
        <div className="lin-group"><i className="lamp green" />{t("What may cross")}</div>
        <div className="lin-row quiet"><span className="lin-title">
          {t("Only chat and delegated work, and only what each connection allows. Documents, tools, credentials and your task board never leave this workspace.")}
        </span></div>
        <div className="lin-row quiet"><span className="lin-title">
          {t("Anything another workspace writes is shown as outside content and can never act as an instruction, even after it is saved or summarised.")}
        </span></div>
        <div className="lin-row quiet"><span className="lin-title">
          {t("A workspace that sends too much is slowed down on its own; it cannot crowd out anyone else.")}
        </span></div>
      </section>
      <section className="lin-list" aria-label={t("Permissions by connection")}>
        <div className="lin-group"><i className="lamp" />{t("Permissions by connection")}</div>
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
              {" · "}{t("until {date}", { date: new Date(item.expires_at).toLocaleDateString() })}
            </span>
            <span className="lin-actions">
              {item.state === "ACTIVE" && (
                <button type="button" className="button ghost small" disabled={busy}
                  onClick={() => act(item, "freeze", "Connection frozen.")}>{t("Freeze")}</button>
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
      <section className="lin-list" aria-label={t("Did not arrive")}>
        <div className="lin-group">
          <i className={`lamp ${(undelivered?.length || stuck.length) ? "red" : "green"}`} />
          {t("Did not arrive")}
          <span className="lin-count">{(undelivered?.length ?? 0) + stuck.length}</span>
        </div>
        {undelivered === null && <div className="skeleton block" />}
        {undelivered?.length === 0 && stuck.length === 0 && (
          <div className="lin-row quiet"><span className="lin-title">{t("Everything sent was delivered.")}</span></div>
        )}
        {undelivered?.map((item) => (
          <div key={item.message_id} className="lin-row">
            <Chip value={item.state === "rejected" ? "REJECTED" : "FAILED"} />
            <span className="lin-title"><Peer id={item.peer_workspace} />: {item.content}</span>
            <span className="flow-aside">{item.reject_code}</span>
            <span className="lin-actions">
              <button type="button" className="button ghost small" disabled={busy}
                onClick={() => void call(`/v1/federation/messages/${item.message_id}/resend`,
                  undefined, "Sent again.").then(() => setUndelivered((list) =>
                  (list ?? []).filter((m) => m.message_id !== item.message_id)))}>
                {t("Resend")}
              </button>
            </span>
          </div>
        ))}
        {stuck.map((item) => (
          <div key={item.federation_job_id} className="lin-row">
            <Chip value="FAILED" />
            <span className="lin-title"><Peer id={item.peer_workspace} />: {item.terms.objective}</span>
            <span className="flow-aside">{item.delivery.last_error}</span>
            <span className="lin-actions">
              <button type="button" className="button ghost small" disabled={busy}
                onClick={() => void call(
                  `/v1/federation/delegations/${item.federation_job_id}/sync?resign=true`,
                  undefined, "Sent again.")}>{t("Resend")}</button>
            </span>
          </div>
        ))}
      </section>
      <GovernanceCard connections={connections} request={request} call={call} busy={busy} />
    </>
  );
}

type Report = {
  report_id: string; accused: string; category: string; case_state: string;
  decision: string | null; created_at: string; also_blocked: boolean;
};
type Notice = {
  report_id: string; category: string; case_state: string; decision: string | null;
  appeal_until: string | null; appealed: boolean;
};
type SecurityEvent = { event_id: number; kind: string; peer: string | null; code: string | null;
  at: string };

const CATEGORIES = ["spam", "abuse", "fraud", "impersonation", "data_leak", "prompt_injection",
  "other"];

function GovernanceCard({ connections, request, call, busy }: {
  connections: Connection[]; request: ApiRequest; call: Call; busy: boolean;
}) {
  const { t } = useI18n();
  const [reports, setReports] = useState<Report[]>([]);
  const [notices, setNotices] = useState<Notice[]>([]);
  const [events, setEvents] = useState<SecurityEvent[]>([]);
  const [target, setTarget] = useState("");
  const [category, setCategory] = useState("spam");
  const [statement, setStatement] = useState("");
  const [block, setBlock] = useState(false);
  const load = useCallback(async () => {
    const [r, n, e] = await Promise.all([
      request<{ items: Report[] }>("/v1/federation/reports").catch(() => ({ items: [] })),
      request<{ items: Notice[] }>("/v1/federation/governance/notices")
        .catch(() => ({ items: [] })),
      request<{ items: SecurityEvent[] }>("/v1/federation/security-events")
        .catch(() => ({ items: [] })),
    ]);
    setReports(r.items ?? []);
    setNotices(n.items ?? []);
    setEvents((e.items ?? []).slice(0, 20));
  }, [request]);
  useEffect(() => { void load(); }, [load]);

  return (
    <>
      <section className="lin-list" aria-label={t("Reports")}>
        <div className="lin-group"><i className="lamp" />{t("Reports")}
          <span className="lin-count">{reports.length}</span></div>
        <form className="deploy-form" onSubmit={async (event) => {
          event.preventDefault();
          if (!target || statement.trim().length < 10) return;
          const done = await call("/v1/federation/reports", {
            accused: target, category, statement: statement.trim(), block,
          }, "Report filed. An operator reviews it; nothing happens to them automatically.");
          if (done) { setStatement(""); setBlock(false); await load(); }
        }}>
          <label>{t("Workspace")}
            <select value={target} onChange={(event) => setTarget(event.target.value)} required>
              <option value="">{t("Choose…")}</option>
              {connections.map((item) => (
                <option key={item.connection_id} value={item.peer_workspace}>
                  {shortId(item.peer_workspace)}
                </option>
              ))}
            </select>
          </label>
          <label>{t("What happened")}
            <select value={category} onChange={(event) => setCategory(event.target.value)}>
              {CATEGORIES.map((c) => <option key={c} value={c}>{t(c)}</option>)}
            </select>
          </label>
          <label className="federation-wide">{t("Describe it (the other side will not see this)")}
            <textarea value={statement} onChange={(event) => setStatement(event.target.value)}
              minLength={10} maxLength={4000} rows={3} required />
          </label>
          <label className="check">
            <input type="checkbox" checked={block} onChange={(event) =>
              setBlock(event.target.checked)} />
            {t("Also block them for this workspace")}
          </label>
          <span className="lin-actions">
            <button type="submit" className="button primary small" disabled={busy}>{t("File report")}</button>
            <span className="flow-aside">
              {t("A report opens a case for this server's operators. It never punishes anyone by itself.")}
            </span>
          </span>
        </form>
        {reports.map((item) => (
          <div key={item.report_id} className="lin-row">
            <span className="flow-aside">{t(item.case_state)}</span>
            <span className="lin-title"><Peer id={item.accused} />: {t(item.category)}</span>
            <span className="flow-aside">{item.decision ?? ""}</span>
          </div>
        ))}
      </section>
      <section className="lin-list" aria-label={t("Decisions about us")}>
        <div className="lin-group"><i className={`lamp ${notices.length ? "amber" : ""}`} />
          {t("Decisions about us")}<span className="lin-count">{notices.length}</span></div>
        {notices.length === 0 && (
          <div className="lin-row quiet"><span className="lin-title">
            {t("None. Only this server's operators can decide; a message claiming a platform verdict is ignored.")}
          </span></div>
        )}
        {notices.map((item) => (
          <div key={item.report_id} className="lin-row">
            <span className="flow-aside">{t(item.case_state)}</span>
            <span className="lin-title">{t(item.category)}: {item.decision}</span>
            <span className="lin-actions">
              {item.case_state === "APPEAL_WINDOW" && !item.appealed && (
                <button type="button" className="button ghost small" disabled={busy} onClick={() => {
                  const text = window.prompt(t("Your appeal (an owner)"));
                  if (text && text.trim().length >= 10) {
                    void call(`/v1/federation/governance/notices/${item.report_id}/appeal`,
                      { statement: text.trim() }, "Appeal filed.").then(() => load());
                  }
                }}>{t("Appeal")}</button>
              )}
            </span>
          </div>
        ))}
      </section>
      <section className="lin-list" aria-label={t("Refused for safety")}>
        <div className="lin-group"><i className={`lamp ${events.length ? "red" : "green"}`} />
          {t("Refused for safety")}<span className="lin-count">{events.length}</span></div>
        {events.length === 0 && (
          <div className="lin-row quiet"><span className="lin-title">{t("Nothing refused yet.")}</span></div>
        )}
        {events.map((item) => (
          <div key={item.event_id} className="lin-row">
            <span className="flow-aside">{new Date(item.at).toLocaleString()}</span>
            <span className="lin-title">{t(item.kind)}</span>
            <span className="flow-aside">{item.peer ? <Peer id={item.peer} /> : null} {item.code}</span>
          </div>
        ))}
      </section>
    </>
  );
}
