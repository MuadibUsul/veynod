"use client";

import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";

import { useI18n } from "../i18n";
import type { ApiRequest } from "../types";
import { relativeTime } from "../types";
import { Spinner } from "./ui";

/*
 * The diagram is the one thing a person decides (spec-diagram-dispatch v3):
 * which chunks exist, how they depend on each other, and which changes go in.
 * Everything a person does to a diagram happens on this page; agents do the
 * rest from the same record.
 */

type DiagramSummary = {
  diagram_key: string;
  title: string;
  namespace: string;
  status: "draft" | "confirmed" | "frozen";
  version: number;
  updated_at: string | null;
};

type Chunk = {
  chunk_key: string;
  title: string;
  scope: string;
  layer: "interface" | "implementation";
  depends_on: string[];
  done_criteria: unknown;
  status: string;
  block_reason: string | null;
  block_kind: string | null;
  task_key: string | null;
  task_status: string | null;
  trust_score: number | null;
  token_budget: number | null;
  token_consumed: number | null;
  review_reject_count: number;
  origin: string;
  increment_of: string | null;
};

type ChangeRow = {
  change_key: string;
  summary: string;
  source: string;
  status?: string;
  decided_by?: string | null;
  decision_kind?: string | null;
  decided_at?: string | null;
  created_at?: string | null;
};

type DiagramDetail = {
  diagram_key: string;
  title: string;
  description: string | null;
  status: DiagramSummary["status"];
  version: number;
  confirmed_by: string | null;
  self_check: { risk?: boolean; missing?: string[] } | null;
  chunks: Chunk[];
  progress: { distribution: Record<string, number>; total: number };
  warnings: Array<{ chunk_key?: string; kind: string; detail: string }>;
  pending_changes: ChangeRow[];
  recent_changes?: ChangeRow[];
};

type ChangeDetail = ChangeRow & {
  request_text: string | null;
  impact: { chunks?: string[]; downstream?: string[]; interfaces?: string[]; tasks?: string[] };
  proposed_by: string;
  from_version: number;
};

// Status → what a person reads, and which tone it gets.
const STATUS: Record<string, { label: string; tone: "idle" | "work" | "wait" | "risk" | "done" }> = {
  pending: { label: "Not started", tone: "idle" },
  dispatched: { label: "Handed out", tone: "work" },
  in_progress: { label: "In progress", tone: "work" },
  awaiting_gate: { label: "Being checked", tone: "wait" },
  awaiting_ci: { label: "Being tested", tone: "wait" },
  awaiting_review: { label: "In review", tone: "wait" },
  rework: { label: "Being redone", tone: "risk" },
  ready_to_merge: { label: "Ready to merge", tone: "wait" },
  queued: { label: "Waiting to merge", tone: "wait" },
  verified: { label: "Merging", tone: "wait" },
  blocked: { label: "Blocked", tone: "risk" },
  blocked_by_spec: { label: "Paused for a change", tone: "risk" },
  merged: { label: "Done", tone: "done" },
  retired: { label: "Removed", tone: "idle" },
};

/** Waiting on earlier chunks or a file is the normal order of work, not trouble. */
function statusOf(chunk: Chunk): { label: string; tone: "idle" | "work" | "wait" | "risk" | "done" } {
  if (chunk.status === "blocked" && chunk.block_kind === "dependency") {
    return { label: "Waiting on earlier chunks", tone: "idle" };
  }
  if (chunk.status === "blocked" && chunk.block_kind === "file_lock") {
    return { label: "Waiting for a file", tone: "wait" };
  }
  return STATUS[chunk.status] ?? { label: chunk.status, tone: "idle" };
}

const DIAGRAM_STATUS: Record<DiagramSummary["status"], string> = {
  draft: "Draft",
  confirmed: "Confirmed",
  frozen: "Frozen",
};

/** Longest path from a chunk with no dependencies: the column it sits in. */
function layers(chunks: Chunk[]): Chunk[][] {
  const byKey = new Map(chunks.map((chunk) => [chunk.chunk_key, chunk]));
  const depth = new Map<string, number>();
  const visit = (key: string, trail: Set<string>): number => {
    if (depth.has(key)) return depth.get(key)!;
    const chunk = byKey.get(key);
    if (!chunk || trail.has(key)) return 0;
    trail.add(key);
    const deps = chunk.depends_on.filter((dep) => byKey.has(dep));
    const value = deps.length ? 1 + Math.max(...deps.map((dep) => visit(dep, trail))) : 0;
    trail.delete(key);
    depth.set(key, value);
    return value;
  };
  chunks.forEach((chunk) => visit(chunk.chunk_key, new Set()));
  const columns: Chunk[][] = [];
  chunks.forEach((chunk) => {
    const column = depth.get(chunk.chunk_key) ?? 0;
    (columns[column] ??= []).push(chunk);
  });
  return columns.filter(Boolean);
}

function storageKey(workspaceId: string) {
  return `veynod.diagram.${workspaceId}`;
}

export function DiagramView({
  workspaceId,
  request,
  sessionMode,
  notify,
  onOpenTask,
}: {
  workspaceId: string;
  request: ApiRequest;
  sessionMode: boolean;
  notify: (message: string) => void;
  onOpenTask: (taskKey: string) => void;
}) {
  const { t } = useI18n();
  const [list, setList] = useState<DiagramSummary[] | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<DiagramDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [openChunk, setOpenChunk] = useState<string | null>(null);
  const [openChange, setOpenChange] = useState<ChangeDetail | null>(null);
  const [note, setNote] = useState("");
  const [asking, setAsking] = useState(false);
  const [requestText, setRequestText] = useState("");
  const [requestTarget, setRequestTarget] = useState("");

  const loadList = useCallback(async () => {
    try {
      const value = await request<{ items: DiagramSummary[] }>("/v1/diagrams");
      setList(value.items);
      setError(null);
      setSelected((current) => {
        if (current && value.items.some((item) => item.diagram_key === current)) return current;
        let remembered: string | null = null;
        try {
          remembered = window.localStorage.getItem(storageKey(workspaceId));
        } catch {
          remembered = null;
        }
        if (remembered && value.items.some((item) => item.diagram_key === remembered)) {
          return remembered;
        }
        return value.items[0]?.diagram_key ?? null;
      });
    } catch (caught) {
      setList([]);
      setError(caught instanceof Error ? caught.message : String(caught));
    }
  }, [request, workspaceId]);

  const loadDetail = useCallback(async (key: string) => {
    try {
      setDetail(await request<DiagramDetail>(`/v1/diagrams/${encodeURIComponent(key)}`));
      setError(null);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    }
  }, [request]);

  useEffect(() => {
    if (!sessionMode) return;
    void loadList();
  }, [loadList, sessionMode]);

  useEffect(() => {
    if (!selected) {
      setDetail(null);
      return;
    }
    try {
      window.localStorage.setItem(storageKey(workspaceId), selected);
    } catch {
      // A private window keeps no memory; the first diagram is shown next time.
    }
    setOpenChunk(null);
    setOpenChange(null);
    void loadDetail(selected);
    const timer = window.setInterval(() => void loadDetail(selected), 15000);
    return () => window.clearInterval(timer);
  }, [selected, loadDetail, workspaceId]);

  async function act(label: string, path: string, body?: unknown, done?: string) {
    if (!detail) return;
    setBusy(label);
    try {
      await request(path, { method: "POST", body: JSON.stringify(body ?? {}) });
      if (done) notify(t(done));
      await Promise.all([loadDetail(detail.diagram_key), loadList()]);
      return true;
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
      return false;
    } finally {
      setBusy(null);
    }
  }

  async function showChange(key: string) {
    if (!detail) return;
    try {
      setOpenChange(await request<ChangeDetail>(
        `/v1/diagrams/${encodeURIComponent(detail.diagram_key)}/changes/${encodeURIComponent(key)}`,
      ));
      setNote("");
      setOpenChunk(null);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    }
  }

  async function decide(decision: "approve" | "reject") {
    if (!detail || !openChange) return;
    const ok = await act(
      decision,
      `/v1/diagrams/${encodeURIComponent(detail.diagram_key)}/changes/${encodeURIComponent(openChange.change_key)}/decide`,
      { decision, note: note.trim() || undefined },
      decision === "approve" ? "Change approved. The diagram moved to its next version." : "Change rejected.",
    );
    if (ok) setOpenChange(null);
  }

  async function sendRequest() {
    if (!detail || !requestText.trim()) return;
    setBusy("ask");
    try {
      // §16 fast lane: the person's words go to the diagram's planner verbatim;
      // the planner turns them into a change with its impact for this page.
      await request("/v1/tasks", {
        method: "POST",
        body: JSON.stringify({
          title: `${t("New request for diagram")} ${detail.title}`.slice(0, 200),
          description: [
            `Diagram: ${detail.diagram_key} (version ${detail.version})`,
            requestTarget ? `About chunk: ${requestTarget}` : null,
            "",
            "The person's request, verbatim:",
            requestText.trim(),
            "",
            `File it with POST /v1/diagrams/${detail.diagram_key}/increments (request_text verbatim, `
              + "a chunk with its contract and done criteria). Work in flight is never rewritten.",
          ].filter((line) => line !== null).join("\n"),
          assigned_role: "ANALYST",
          priority: 50,
        }),
      });
      notify(t("Request sent. It comes back here as a change for you to decide."));
      setRequestText("");
      setRequestTarget("");
      setAsking(false);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy(null);
    }
  }

  const visibleChunks = useMemo(
    () => (detail?.chunks ?? []).filter((chunk) => chunk.status !== "retired"),
    [detail],
  );
  const columns = useMemo(() => layers(visibleChunks), [visibleChunks]);
  const done = detail?.progress.distribution.merged ?? 0;
  const total = detail?.progress.total ?? 0;
  const chunk = visibleChunks.find((item) => item.chunk_key === openChunk) ?? null;

  if (!sessionMode) {
    return (
      <section className="focus-empty">
        <h2>{t("Diagrams are set by the workspace's people")}</h2>
        <p>{t("Sign in with a member account of this workspace to see and decide its diagrams.")}</p>
      </section>
    );
  }

  if (list === null) {
    return (
      <div className="dg-skeleton" aria-busy="true">
        <div className="skeleton line w40" />
        <div className="dg-skeleton-columns">
          <div className="skeleton block" />
          <div className="skeleton block" />
          <div className="skeleton block" />
        </div>
      </div>
    );
  }

  if (list.length === 0) {
    return (
      <section className="focus-empty">
        <h2>{t("No diagram yet")}</h2>
        <p>
          {t("A diagram splits the project into chunks with clear contracts and order. Ask your planning agent to draft one; it appears here for you to confirm.")}
        </p>
        {error && <div className="inline-error">{error}</div>}
      </section>
    );
  }

  return (
    <div className="dg">
      {list.length > 1 && (
        <nav className="dg-picker" aria-label={t("Diagrams")}>
          {list.map((item) => (
            <button
              key={item.diagram_key}
              type="button"
              className={`dg-pick${item.diagram_key === selected ? " active" : ""}`}
              aria-current={item.diagram_key === selected ? "page" : undefined}
              onClick={() => setSelected(item.diagram_key)}
            >
              <span>{item.title}</span>
              <small>{t(DIAGRAM_STATUS[item.status])}</small>
            </button>
          ))}
        </nav>
      )}

      {error && <div className="inline-error" role="alert">{error}</div>}

      {!detail ? (
        <div className="dg-skeleton" aria-busy="true"><div className="skeleton block" /></div>
      ) : (
        <>
          <header className="dg-head">
            <div className="dg-title">
              <span className={`dg-state ${detail.status}`}>{t(DIAGRAM_STATUS[detail.status])}</span>
              <h2>{detail.title}</h2>
              <p>
                {t("Version")} {detail.version}
                {total > 0 && <> · {done}/{total} {t("chunks done")}</>}
              </p>
              {total > 0 && (
                <div className="dg-progress" role="progressbar" aria-valuemin={0}
                  aria-valuemax={total} aria-valuenow={done}>
                  <span style={{ transform: `scaleX(${total ? done / total : 0})` }} />
                </div>
              )}
            </div>
            <div className="dg-actions">
              {detail.status === "draft" && (
                <button type="button" className="button primary" disabled={busy !== null}
                  onClick={() => void act("confirm",
                    `/v1/diagrams/${encodeURIComponent(detail.diagram_key)}/confirm`,
                    undefined, "Diagram confirmed. Freeze it when the plan is final.")}>
                  {busy === "confirm" ? <Spinner /> : null}{t("Confirm this diagram")}
                </button>
              )}
              {detail.status === "confirmed" && (
                <button type="button" className="button primary" disabled={busy !== null}
                  onClick={() => void act("freeze",
                    `/v1/diagrams/${encodeURIComponent(detail.diagram_key)}/freeze`,
                    undefined, "Baseline frozen. Agents can now start on the chunks.")}>
                  {busy === "freeze" ? <Spinner /> : null}{t("Freeze and start")}
                </button>
              )}
              <button type="button" className="button ghost" onClick={() => setAsking((v) => !v)}
                aria-expanded={asking}>
                {t("Ask for a change")}
              </button>
            </div>
          </header>

          {detail.description && <p className="dg-description">{detail.description}</p>}

          {detail.status === "draft" && detail.self_check?.risk && (
            <div className="dg-note">
              {t("The planner left parts of the self check open. You can still confirm it.")}
            </div>
          )}

          {asking && (
            <form className="dg-ask" onSubmit={(event) => { event.preventDefault(); void sendRequest(); }}>
              <label className="field">
                <span>{t("What should change? Say it in your own words.")}</span>
                <textarea value={requestText} rows={3} required maxLength={4000}
                  onChange={(event) => setRequestText(event.target.value)}
                  placeholder={t("For example: the export page also needs a CSV download.")} />
              </label>
              <label className="field">
                <span>{t("About a chunk (optional)")}</span>
                <select value={requestTarget} onChange={(event) => setRequestTarget(event.target.value)}>
                  <option value="">{t("The whole diagram")}</option>
                  {visibleChunks.map((item) => (
                    <option key={item.chunk_key} value={item.chunk_key}>{item.title}</option>
                  ))}
                </select>
              </label>
              <div className="dg-ask-actions">
                <button type="submit" className="button primary" disabled={busy !== null || !requestText.trim()}>
                  {busy === "ask" ? <Spinner /> : null}{t("Send to the planner")}
                </button>
                <small>{t("Work already in progress is never rewritten.")}</small>
              </div>
            </form>
          )}

          {detail.pending_changes.length > 0 && (
            <section className="dg-decide" aria-label={t("Waiting for your decision")}>
              <h3>{t("Waiting for your decision")}</h3>
              <ul>
                {detail.pending_changes.map((change) => (
                  <li key={change.change_key}>
                    <button type="button" className="dg-change"
                      onClick={() => void showChange(change.change_key)}>
                      <span>{change.summary}</span>
                      <small>
                        {change.source === "human_request" ? t("Your request") : t("Proposed by an agent")}
                        {change.created_at ? ` · ${relativeTime(change.created_at)}` : ""}
                      </small>
                    </button>
                  </li>
                ))}
              </ul>
            </section>
          )}

          <div className="dg-body">
            <DiagramCanvas
              columns={columns}
              selected={openChunk}
              onSelect={(key) => { setOpenChunk(key); setOpenChange(null); }}
            />

            {(chunk || openChange) && (
              <aside className="dg-side" aria-live="polite">
                {chunk && (
                  <ChunkPanel chunk={chunk} onClose={() => setOpenChunk(null)} onOpenTask={onOpenTask} />
                )}
                {openChange && (
                  <div className="dg-panel">
                    <div className="dg-panel-head">
                      <h3>{openChange.summary}</h3>
                      <button type="button" className="icon-button" aria-label={t("Close")}
                        onClick={() => setOpenChange(null)}>×</button>
                    </div>
                    {openChange.request_text && (
                      <blockquote className="dg-quote">{openChange.request_text}</blockquote>
                    )}
                    <dl className="dg-facts">
                      <dt>{t("Changes")}</dt>
                      <dd>{(openChange.impact.chunks ?? []).join(", ") || "—"}</dd>
                      <dt>{t("Also affects")}</dt>
                      <dd>{(openChange.impact.downstream ?? []).join(", ") || t("Nothing else")}</dd>
                      {(openChange.impact.interfaces ?? []).length > 0 && (
                        <>
                          <dt>{t("Contracts touched")}</dt>
                          <dd>{(openChange.impact.interfaces ?? []).join(", ")}</dd>
                        </>
                      )}
                      <dt>{t("Proposed by")}</dt>
                      <dd>{openChange.proposed_by}</dd>
                    </dl>
                    <label className="field">
                      <span>{t("Note (optional)")}</span>
                      <input value={note} maxLength={500} onChange={(event) => setNote(event.target.value)} />
                    </label>
                    <div className="dg-panel-actions">
                      <button type="button" className="button primary" disabled={busy !== null}
                        onClick={() => void decide("approve")}>
                        {busy === "approve" ? <Spinner /> : null}{t("Approve")}
                      </button>
                      <button type="button" className="button ghost" disabled={busy !== null}
                        onClick={() => void decide("reject")}>
                        {busy === "reject" ? <Spinner /> : null}{t("Reject")}
                      </button>
                    </div>
                  </div>
                )}
              </aside>
            )}
          </div>

          {(detail.recent_changes ?? []).length > 0 && (
            <details className="dg-history">
              <summary>{t("Recent decisions")}</summary>
              <ul>
                {(detail.recent_changes ?? []).map((change) => (
                  <li key={change.change_key}>
                    <span className={`dg-decision ${change.status}`}>{t(change.status ?? "")}</span>
                    <span className="dg-history-text">{change.summary}</span>
                    <small>
                      {change.decision_kind === "super_admin_for_owner"
                        ? t("decided for the owner")
                        : (change.decided_by ?? "").startsWith("human:")
                          ? t("a person")
                          : change.decided_by}
                      {change.decided_at ? ` · ${relativeTime(change.decided_at)}` : ""}
                    </small>
                    {change.status === "approved" && (
                      <button type="button" className="button ghost small" disabled={busy !== null}
                        onClick={() => void act("revoke",
                          `/v1/diagrams/${encodeURIComponent(detail.diagram_key)}/changes/${encodeURIComponent(change.change_key)}/revoke`,
                          {}, "Decision revoked. Unclaimed work from it was withdrawn.")}>
                        {t("Revoke")}
                      </button>
                    )}
                  </li>
                ))}
              </ul>
            </details>
          )}
        </>
      )}
    </div>
  );
}

function DiagramCanvas({
  columns,
  selected,
  onSelect,
}: {
  columns: Chunk[][];
  selected: string | null;
  onSelect: (key: string) => void;
}) {
  const { t } = useI18n();
  const wrap = useRef<HTMLDivElement | null>(null);
  const cards = useRef(new Map<string, HTMLButtonElement>());
  const [edges, setEdges] = useState<Array<{ id: string; d: string; risk: boolean }>>([]);
  const [size, setSize] = useState({ width: 0, height: 0 });

  const draw = useCallback(() => {
    const root = wrap.current;
    if (!root) return;
    const box = root.getBoundingClientRect();
    const next: Array<{ id: string; d: string; risk: boolean }> = [];
    columns.flat().forEach((chunk) => {
      const to = cards.current.get(chunk.chunk_key);
      if (!to) return;
      chunk.depends_on.forEach((dep) => {
        const from = cards.current.get(dep);
        if (!from) return;
        const a = from.getBoundingClientRect();
        const b = to.getBoundingClientRect();
        const x1 = a.right - box.left + root.scrollLeft;
        const y1 = a.top + a.height / 2 - box.top + root.scrollTop;
        const x2 = b.left - box.left + root.scrollLeft;
        const y2 = b.top + b.height / 2 - box.top + root.scrollTop;
        const bend = Math.max(24, (x2 - x1) / 2);
        next.push({
          id: `${dep}->${chunk.chunk_key}`,
          d: `M${x1},${y1} C${x1 + bend},${y1} ${x2 - bend},${y2} ${x2},${y2}`,
          risk: statusOf(chunk).tone === "risk",
        });
      });
    });
    setEdges(next);
    setSize({ width: root.scrollWidth, height: root.scrollHeight });
  }, [columns]);

  useLayoutEffect(() => {
    draw();
    const root = wrap.current;
    if (!root || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => draw());
    observer.observe(root);
    return () => observer.disconnect();
  }, [draw]);

  if (columns.length === 0) {
    return <div className="focus-empty small">{t("This diagram has no chunks yet.")}</div>;
  }

  return (
    <div className="dg-canvas" ref={wrap}>
      <svg className="dg-edges" width={size.width} height={size.height} aria-hidden="true">
        {edges.map((edge) => (
          <path key={edge.id} d={edge.d} className={edge.risk ? "risk" : undefined} />
        ))}
      </svg>
      {columns.map((column, index) => (
        <div className="dg-column" key={index}>
          <div className="dg-column-label">
            {index === 0 ? t("Start here") : t("Step {n}", { n: index + 1 })}
          </div>
          {column.map((chunk) => {
            const status = statusOf(chunk);
            return (
              <button
                key={chunk.chunk_key}
                type="button"
                ref={(node) => {
                  if (node) cards.current.set(chunk.chunk_key, node);
                  else cards.current.delete(chunk.chunk_key);
                }}
                className={`dg-chunk tone-${status.tone}${selected === chunk.chunk_key ? " selected" : ""}`}
                aria-pressed={selected === chunk.chunk_key}
                onClick={() => onSelect(chunk.chunk_key)}
              >
                <span className="dg-chunk-top">
                  {chunk.layer === "interface" && <span className="dg-tag">{t("Contract")}</span>}
                  <span className={`dg-status tone-${status.tone}`}>{t(status.label)}</span>
                </span>
                <strong>{chunk.title}</strong>
                {chunk.block_reason && (status.tone === "risk") && (
                  <small className="dg-reason">{chunk.block_reason}</small>
                )}
              </button>
            );
          })}
        </div>
      ))}
    </div>
  );
}

function ChunkPanel({
  chunk,
  onClose,
  onOpenTask,
}: {
  chunk: Chunk;
  onClose: () => void;
  onOpenTask: (taskKey: string) => void;
}) {
  const { t } = useI18n();
  const status = statusOf(chunk);
  const criteria = Array.isArray(chunk.done_criteria)
    ? chunk.done_criteria.map((item) =>
        typeof item === "string" ? item : (item as { text?: string })?.text ?? JSON.stringify(item))
    : [];
  return (
    <div className="dg-panel">
      <div className="dg-panel-head">
        <h3>{chunk.title}</h3>
        <button type="button" className="icon-button" aria-label={t("Close")} onClick={onClose}>×</button>
      </div>
      <span className={`dg-status tone-${status.tone}`}>{t(status.label)}</span>
      {chunk.scope && <p className="dg-scope">{chunk.scope}</p>}
      {chunk.block_reason && (
        <div className={`dg-note${status.tone === "risk" ? " risk" : ""}`}>{chunk.block_reason}</div>
      )}
      <dl className="dg-facts">
        <dt>{t("Needs first")}</dt>
        <dd>{chunk.depends_on.length ? chunk.depends_on.join(", ") : t("Nothing")}</dd>
        {criteria.length > 0 && (
          <>
            <dt>{t("Done when")}</dt>
            <dd>
              <ul>{criteria.slice(0, 6).map((item, index) => <li key={index}>{item}</li>)}</ul>
            </dd>
          </>
        )}
        {chunk.review_reject_count > 0 && (
          <>
            <dt>{t("Sent back")}</dt>
            <dd>{chunk.review_reject_count}×</dd>
          </>
        )}
      </dl>
      {chunk.task_key && (
        <button type="button" className="button ghost small" onClick={() => onOpenTask(chunk.task_key!)}>
          {t("Open its task")}
        </button>
      )}
    </div>
  );
}
