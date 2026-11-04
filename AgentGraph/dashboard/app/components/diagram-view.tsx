"use client";

import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type PointerEvent as ReactPointerEvent } from "react";

import { useI18n } from "../i18n";
import type { ApiRequest } from "../types";
import { relativeTime } from "../types";
import { AgentFace } from "./agent-face";
import { STATE_LABEL, waited, type FlowItem, type Lane, type Overview } from "./flow-view";
import { Spinner } from "./ui";
import { useWorkspaceStream } from "./use-workspace-stream";

/*
 * The diagram is the one thing a person decides (spec-diagram-dispatch v3):
 * which chunks exist, how they depend on each other, and which changes go in.
 * Everything a person does to a diagram happens on this page; agents do the
 * rest from the same record.
 *
 * It is also where the work is seen happening (v3.24): the live layer draws on
 * each chunk its one state and how long it has been in it, who works on it and
 * on which computer, what it still waits for, and along each edge what an
 * upstream chunk delivered. The plan layer shows the contracts instead. "All
 * diagrams" lists every plan, including work not drawn as a diagram yet.
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
  merge?: {
    at?: string;
    by?: string;
    delivered?: { docs?: { uri?: string; doc_key?: string; version?: number }[]; files?: { path: string }[] } | null;
  } | null;
};

type Layer = "live" | "plan";

// Why the Loop could not give a chunk's task to a computer, short.
const UNPLACED_SHORT: Record<string, string> = {
  no_host_online: "No computer is online",
  no_host_for_role: "No computer takes this kind of task",
  no_host_for_namespace: "No computer may work here",
  no_host_with_capabilities: "No computer has the skills",
  no_host_for_repo: "No computer reaches its repository",
  no_kernel_binding: "Its repository is not connected to a Kernel",
  no_host_with_runtime: "No computer has Claude Code or Codex ready",
  every_host_failed: "Every computer already failed it",
  hosts_out_of_quota: "Computers are out of quota",
  no_reviewer_host: "No computer can review it",
};

/** What an upstream chunk handed over, named briefly for its edges. */
function deliveredLabel(chunk: Chunk): string | null {
  const delivered = chunk.merge?.delivered;
  if (!delivered) return null;
  const names = [
    ...(delivered.files ?? []).map((file) => file.path.split("/").pop() ?? file.path),
    ...(delivered.docs ?? []).map((doc) => {
      const name = doc.doc_key ?? doc.uri?.replace(/^doc:\/\//, "") ?? "";
      return name.split("/").pop() || name;
    }).filter(Boolean),
  ];
  if (!names.length) return null;
  return names.length > 2 ? `${names.slice(0, 2).join(" · ")} +${names.length - 2}` : names.join(" · ");
}

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
  policy?: { mode?: string };
  mode?: string;
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

type Panel = "chunk" | "change" | "decide" | "ask" | "history" | "about" | null;

// Canvas geometry: one column per dependency step, read left to right.
const NODE_W = 240;
const NODE_H = 92;
const COL_GAP = 120;
const ROW_GAP = 28;
const MIN_ZOOM = 0.25;
const MAX_ZOOM = 2;

type Placed = { chunk: Chunk; x: number; y: number };

function place(columns: Chunk[][]): { nodes: Placed[]; width: number; height: number } {
  const rows = Math.max(1, ...columns.map((column) => column.length));
  const span = rows * NODE_H + (rows - 1) * ROW_GAP;
  const nodes: Placed[] = [];
  columns.forEach((column, index) => {
    const height = column.length * NODE_H + (column.length - 1) * ROW_GAP;
    const top = (span - height) / 2;
    column.forEach((chunk, row) => {
      nodes.push({ chunk, x: index * (NODE_W + COL_GAP), y: top + row * (NODE_H + ROW_GAP) });
    });
  });
  return { nodes, width: columns.length * (NODE_W + COL_GAP) - COL_GAP, height: span };
}

function layerKey(workspaceId: string) {
  return `veynod.diagram.layer.${workspaceId}`;
}

function storageKey(workspaceId: string) {
  return `veynod.diagram.${workspaceId}`;
}

export function DiagramView({
  workspaceId,
  apiBase,
  credential,
  request,
  sessionMode,
  notify,
  onOpenTask,
  onOpenLoop,
  focus = null,
}: {
  workspaceId: string;
  apiBase: string;
  credential: string;
  request: ApiRequest;
  sessionMode: boolean;
  notify: (message: string) => void;
  onOpenTask: (taskKey: string) => void;
  onOpenLoop: () => void;
  /** Open this diagram (and chunk), e.g. from the Loop page; `at` makes a repeat count. */
  focus?: { diagram: string; chunk?: string; at: number } | null;
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
  const [panel, setPanel] = useState<Panel>(null);
  const [overview, setOverview] = useState<Overview | null>(null);
  const [unplaced, setUnplaced] = useState<Map<string, string>>(new Map());
  const [portfolio, setPortfolio] = useState(false);
  const [layer, setLayer] = useState<Layer>(() => {
    try {
      return window.localStorage.getItem(layerKey(workspaceId)) === "plan" ? "plan" : "live";
    } catch {
      return "live";
    }
  });
  const pendingChunk = useRef<string | null>(null);
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

  // Who works on each chunk, where and since when, from the Loop's one read.
  const loadLive = useCallback(async () => {
    try {
      setOverview(await request<Overview>("/v1/loop/overview"));
    } catch {
      // the plan still shows; the live layer fills in on the next event
    }
    try {
      const placements = await request<{ needs_you: { task_key: string | null; reason: string }[] }>(
        "/v1/runner/placements?open_only=true",
      );
      setUnplaced(new Map(placements.needs_you.filter((item) => item.task_key)
        .map((item) => [item.task_key as string, item.reason])));
    } catch {
      setUnplaced(new Map());
    }
  }, [request]);

  useEffect(() => {
    if (!sessionMode) return;
    void loadList();
    void loadLive();
  }, [loadList, loadLive, sessionMode]);

  useEffect(() => {
    try {
      window.localStorage.setItem(layerKey(workspaceId), layer);
    } catch {
      // per-viewer convenience only
    }
  }, [layer, workspaceId]);

  // Opened from elsewhere (the Loop page): show that diagram, then that chunk.
  useEffect(() => {
    if (!focus) return;
    setPortfolio(false);
    pendingChunk.current = focus.chunk ?? null;
    setSelected(focus.diagram);
  }, [focus]);

  // Changes arrive on the workspace stream; a burst is one reload.
  const reload = useRef<number | undefined>(undefined);
  const onStream = useCallback((event: { event_type?: string; type?: string | null; table?: string }) => {
    const kind = event.event_type || event.type || "";
    if (!/^(CHUNK_|DIAGRAM_|TASK_|SUBGRAPH_|HOST_|PLACEMENT_|DEPLOY_)/.test(kind) && event.table !== "collab_tasks") return;
    window.clearTimeout(reload.current);
    reload.current = window.setTimeout(() => {
      if (selected) void loadDetail(selected);
      void loadLive();
    }, 800);
  }, [selected, loadDetail, loadLive]);
  useEffect(() => () => window.clearTimeout(reload.current), []);
  const live = useWorkspaceStream({
    apiBase,
    credential,
    path: "/v1/stream?tables=collab_tasks,timeline_streams",
    onEvent: onStream,
    enabled: sessionMode,
  });

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
    const wanted = pendingChunk.current;
    pendingChunk.current = null;
    setOpenChunk(wanted);
    setOpenChange(null);
    setPanel(wanted ? "chunk" : null);
    void loadDetail(selected);
    // The stream keeps it current; this only covers a dropped stream.
    const timer = window.setInterval(() => void loadDetail(selected), 60000);
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
      setPanel("change");
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
    if (ok) {
      setOpenChange(null);
      setPanel(null);
    }
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
      setPanel(null);
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
  const pending = detail?.pending_changes ?? [];
  const history = detail?.recent_changes ?? [];
  const lane = overview?.lanes.find((item) => item.lane_id === `diagram:${detail?.diagram_key}`);
  const liveByChunk = useMemo(
    () => new Map((lane?.items ?? []).map((item) => [item.key, item])),
    [lane],
  );

  function openPanel(next: Panel) {
    setPanel((current) => (current === next ? null : next));
  }

  function ask(target: string | null) {
    setRequestTarget(target ?? "");
    setPanel("ask");
  }

  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if (event.key === "Escape") {
        setPanel(null);
        setOpenChunk(null);
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const stage = useRef<HTMLDivElement | null>(null);
  const [stageHeight, setStageHeight] = useState<number | null>(null);
  useLayoutEffect(() => {
    // The canvas takes the rest of the window, whatever sits above it.
    const fit = () => {
      const top = stage.current?.getBoundingClientRect().top ?? 0;
      setStageHeight(Math.max(360, window.innerHeight - top - 12));
    };
    fit();
    window.addEventListener("resize", fit);
    return () => window.removeEventListener("resize", fit);
  }, [list, detail === null]);

  if (!sessionMode) {
    return (
      <section className="focus-empty">
        <h2>{t("Diagrams are set by the workspace's people")}</h2>
        <p>{t("Sign in with a member account of this workspace to see and decide its diagrams.")}</p>
      </section>
    );
  }

  if (list === null) {
    return <div className="dgx dgx-loading" aria-busy="true"><div className="skeleton block" /></div>;
  }

  if (list.length === 0) {
    return (
      <section className="focus-empty">
        <h2>{t("No diagram yet")}</h2>
        <p>
          {t("A diagram splits the project into chunks with clear contracts and order. Ask your planning agent to draft one; it appears here for you to confirm.")}
        </p>
        {error && <div className="inline-error">{error}</div>}
        {overview && overview.lanes.length > 0 && (
          <Portfolio diagrams={[]} lanes={overview.lanes} onOpenDiagram={() => undefined} onOpenLoop={onOpenLoop} />
        )}
      </section>
    );
  }

  return (
    <div className="dgx" ref={stage} style={stageHeight ? { height: stageHeight } : undefined}>
      {detail && (
        <InfiniteCanvas
          key={detail.diagram_key}
          columns={columns}
          layer={layer}
          live={liveByChunk}
          unplaced={unplaced}
          selected={openChunk}
          onSelect={(key) => {
            setOpenChunk(key);
            setOpenChange(null);
            setPanel(key ? "chunk" : null);
          }}
          onAsk={ask}
        />
      )}

      <div className="dgx-float dgx-top-left">
        <button type="button" className={`dgx-pill${portfolio ? " on" : ""}`} aria-pressed={portfolio}
          onClick={() => setPortfolio((value) => !value)}>
          {t("All diagrams")}
        </button>
        {list.length > 1 ? (
          <select
            className="dgx-switch"
            aria-label={t("Diagrams")}
            value={selected ?? ""}
            onChange={(event) => setSelected(event.target.value)}
          >
            {list.map((item) => (
              <option key={item.diagram_key} value={item.diagram_key}>{item.title}</option>
            ))}
          </select>
        ) : (
          <strong className="dgx-name">{detail?.title}</strong>
        )}
        {detail && (
          <>
            <span className={`dg-state ${detail.status}`}>{t(DIAGRAM_STATUS[detail.status])}</span>
            <span className="dgx-meta">
              v{detail.version}
              {total > 0 && <> · {done}/{total}</>}
            </span>
            <span className="dgx-layers" role="group" aria-label={t("Layer")}>
              <button type="button" aria-pressed={layer === "live"} onClick={() => setLayer("live")}>
                <i className={`lamp ${live ? "cyan" : ""}`} />{t("Live")}
              </button>
              <button type="button" aria-pressed={layer === "plan"} onClick={() => setLayer("plan")}>
                {t("Plan")}
              </button>
            </span>
            <select className="dgx-mode" aria-label={t("Automation for this diagram")}
              title={t("How far the Loop may go on this diagram")}
              value={detail.policy?.mode ?? ""} disabled={busy !== null}
              onChange={(event) => void act("policy",
                `/v1/diagrams/${encodeURIComponent(detail.diagram_key)}/policy`,
                { mode: event.target.value || null }, "Saved. The Loop follows it from the next step.")}>
              <option value="">{t("Automation: as the workspace")}</option>
              <option value="manual">{t("Automation: ask me every step")}</option>
              <option value="semi">{t("Automation: small calls automatically")}</option>
              <option value="full">{t("Automation: do it all")}</option>
            </select>
            {(detail.description || detail.self_check?.risk) && (
              <button type="button" className="dgx-icon" aria-label={t("About this diagram")}
                aria-pressed={panel === "about"} onClick={() => openPanel("about")}>i</button>
            )}
          </>
        )}
      </div>

      {detail && (
        <div className="dgx-float dgx-top-right">
          {pending.length > 0 && (
            <button type="button" className={`dgx-pill attention${panel === "decide" || panel === "change" ? " on" : ""}`}
              onClick={() => openPanel("decide")}>
              {t("Waiting for your decision")} <b>{pending.length}</b>
            </button>
          )}
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
          <button type="button" className={`button ghost${panel === "ask" ? " active" : ""}`}
            onClick={() => (panel === "ask" ? setPanel(null) : ask(openChunk))}>
            {t("Ask for a change")}
          </button>
        </div>
      )}

      {detail && total > 0 && (
        <div className="dgx-progress" role="progressbar" aria-valuemin={0} aria-valuemax={total}
          aria-valuenow={done} aria-label={t("chunks done")}>
          <span style={{ transform: `scaleX(${done / total})` }} />
        </div>
      )}

      {history.length > 0 && (
        <div className="dgx-float dgx-bottom-right">
          <button type="button" className={`dgx-pill${panel === "history" ? " on" : ""}`}
            onClick={() => openPanel("history")}>
            {t("Recent decisions")}
          </button>
        </div>
      )}

      {error && <div className="dgx-error" role="alert">{error}</div>}

      {portfolio && overview && (
        <div className="dgx-portfolio-wrap" role="dialog" aria-label={t("All diagrams")}>
          <Portfolio
            diagrams={list}
            lanes={overview.lanes}
            onOpenDiagram={(key) => {
              setPortfolio(false);
              setSelected(key);
            }}
            onOpenLoop={onOpenLoop}
          />
        </div>
      )}

      {detail && panel && (
        <aside className="dgx-panel" aria-live="polite">
          <button type="button" className="dgx-close" aria-label={t("Close")}
            onClick={() => { setPanel(null); setOpenChunk(null); }}>×</button>

          {panel === "about" && (
            <div className="dgx-section">
              <h3>{detail.title}</h3>
              {detail.description && <p className="dgx-text">{detail.description}</p>}
              {detail.status === "draft" && detail.self_check?.risk && (
                <p className="dgx-text muted">
                  {t("The planner left parts of the self check open. You can still confirm it.")}
                </p>
              )}
            </div>
          )}

          {panel === "chunk" && chunk && (
            <ChunkPanel chunk={chunk} live={liveByChunk.get(chunk.chunk_key)}
              unplacedReason={chunk.task_key ? unplaced.get(chunk.task_key) : undefined}
              onOpenTask={onOpenTask} onAsk={() => ask(chunk.chunk_key)} />
          )}

          {panel === "decide" && (
            <div className="dgx-section">
              <h3>{t("Waiting for your decision")}</h3>
              <ul className="dgx-list">
                {pending.map((change) => (
                  <li key={change.change_key}>
                    <button type="button" className="dg-change" onClick={() => void showChange(change.change_key)}>
                      <span>{change.summary}</span>
                      <small>
                        {change.source === "human_request" ? t("Your request") : t("Proposed by an agent")}
                        {change.created_at ? ` · ${relativeTime(change.created_at)}` : ""}
                      </small>
                    </button>
                  </li>
                ))}
              </ul>
            </div>
          )}

          {panel === "change" && openChange && (
            <div className="dgx-section">
              <h3>{openChange.summary}</h3>
              {openChange.request_text && <blockquote className="dg-quote">{openChange.request_text}</blockquote>}
              <dl className="dg-facts">
                <dt>{t("Changes")}</dt>
                <dd>{titlesOf(openChange.impact.chunks, visibleChunks) || "—"}</dd>
                <dt>{t("Also affects")}</dt>
                <dd>{titlesOf(openChange.impact.downstream, visibleChunks) || t("Nothing else")}</dd>
                {(openChange.impact.interfaces ?? []).length > 0 && (
                  <>
                    <dt>{t("Contracts touched")}</dt>
                    <dd>{titlesOf(openChange.impact.interfaces, visibleChunks)}</dd>
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

          {panel === "ask" && (
            <form className="dgx-section" onSubmit={(event) => { event.preventDefault(); void sendRequest(); }}>
              <h3>{t("What should change?")}</h3>
              <textarea className="dgx-think" value={requestText} rows={8} required maxLength={4000} autoFocus
                aria-label={t("What should change? Say it in your own words.")}
                onChange={(event) => setRequestText(event.target.value)}
                placeholder={t("Say it in your own words. For example: the export page also needs a CSV download.")} />
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

          {panel === "history" && (
            <div className="dgx-section">
              <h3>{t("Recent decisions")}</h3>
              <ul className="dgx-list">
                {history.map((change) => (
                  <li key={change.change_key} className="dgx-history">
                    <span className={`dg-decision ${change.status}`}>{t(change.status ?? "")}</span>
                    <span>{change.summary}</span>
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
            </div>
          )}
        </aside>
      )}
    </div>
  );
}

function titlesOf(keys: string[] | undefined, chunks: Chunk[]): string {
  const title = new Map(chunks.map((item) => [item.chunk_key, item.title]));
  return (keys ?? []).map((key) => title.get(key) ?? key).join(", ");
}

/**
 * A plane to think on: drag or scroll to move, pinch or Ctrl/Cmd + scroll to
 * zoom, double click a chunk (or empty space) to ask for a change there.
 */
function InfiniteCanvas({
  columns,
  layer,
  live,
  unplaced,
  selected,
  onSelect,
  onAsk,
}: {
  columns: Chunk[][];
  layer: Layer;
  live: Map<string, FlowItem>;
  unplaced: Map<string, string>;
  selected: string | null;
  onSelect: (key: string | null) => void;
  onAsk: (target: string | null) => void;
}) {
  const { t } = useI18n();
  const box = useRef<HTMLDivElement | null>(null);
  const layout = useMemo(() => place(columns), [columns]);
  const [view, setView] = useState({ x: 0, y: 0, k: 1 });
  const drag = useRef<{ id: number; x: number; y: number; moved: boolean } | null>(null);
  const fitted = useRef(false);

  const fit = useCallback(() => {
    const el = box.current;
    if (!el) return;
    const { width, height } = el.getBoundingClientRect();
    const pad = 56;
    const k = Math.min(
      1,
      Math.max(MIN_ZOOM, Math.min((width - pad * 2) / Math.max(layout.width, 1),
                                  (height - pad * 2) / Math.max(layout.height, 1))),
    );
    if (k < 0.6) {
      // Too big to read whole on this screen: start at the first step, readable.
      setView({ k: 0.6, x: 16, y: (height - layout.height * 0.6) / 2 });
      return;
    }
    setView({ k, x: (width - layout.width * k) / 2, y: (height - layout.height * k) / 2 });
  }, [layout]);

  useLayoutEffect(() => {
    if (!fitted.current) {
      fit();
      fitted.current = true;
    }
  }, [fit]);

  // Keep the chosen chunk in sight when the side panel opens over the canvas.
  useEffect(() => {
    const node = layout.nodes.find((item) => item.chunk.chunk_key === selected);
    const el = box.current;
    if (!node || !el) return;
    const { width } = el.getBoundingClientRect();
    const covered = width > 640 ? Math.min(400, width - 24) : 0;
    setView((current) => {
      const right = current.x + (node.x + NODE_W) * current.k;
      const left = current.x + node.x * current.k;
      const limit = width - covered - 24;
      if (right <= limit) return current;
      return { ...current, x: current.x - Math.min(right - limit, left - 24) };
    });
  }, [selected, layout]);

  const zoomAt = useCallback((factor: number, cx: number, cy: number) => {
    setView((current) => {
      const k = Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, current.k * factor));
      const ratio = k / current.k;
      return { k, x: cx - (cx - current.x) * ratio, y: cy - (cy - current.y) * ratio };
    });
  }, []);

  useEffect(() => {
    const el = box.current;
    if (!el) return;
    function onWheel(event: WheelEvent) {
      event.preventDefault();
      const rect = el!.getBoundingClientRect();
      if (event.ctrlKey || event.metaKey) {
        zoomAt(Math.exp(-event.deltaY * 0.01), event.clientX - rect.left, event.clientY - rect.top);
      } else {
        setView((current) => ({ ...current, x: current.x - event.deltaX, y: current.y - event.deltaY }));
      }
    }
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, [zoomAt]);

  function onPointerDown(event: ReactPointerEvent<HTMLDivElement>) {
    if ((event.target as HTMLElement).closest(".dgx-node, .dgx-zoom")) return;
    drag.current = { id: event.pointerId, x: event.clientX, y: event.clientY, moved: false };
    event.currentTarget.setPointerCapture(event.pointerId);
  }
  function onPointerMove(event: ReactPointerEvent<HTMLDivElement>) {
    const state = drag.current;
    if (!state || state.id !== event.pointerId) return;
    const dx = event.clientX - state.x;
    const dy = event.clientY - state.y;
    if (Math.abs(dx) + Math.abs(dy) > 2) state.moved = true;
    state.x = event.clientX;
    state.y = event.clientY;
    setView((current) => ({ ...current, x: current.x + dx, y: current.y + dy }));
  }
  function onPointerUp(event: ReactPointerEvent<HTMLDivElement>) {
    const state = drag.current;
    drag.current = null;
    if (state && !state.moved) onSelect(null);
    event.currentTarget.releasePointerCapture?.(event.pointerId);
  }

  const center = () => {
    const rect = box.current?.getBoundingClientRect();
    return rect ? [rect.width / 2, rect.height / 2] as const : [0, 0] as const;
  };
  const byKey = new Map(layout.nodes.map((node) => [node.chunk.chunk_key, node]));
  const grid = 24 * view.k;

  return (
    <div
      className="dgx-canvas"
      ref={box}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      onPointerCancel={() => { drag.current = null; }}
      onDoubleClick={(event) => {
        if (!(event.target as HTMLElement).closest(".dgx-node, .dgx-zoom")) onAsk(null);
      }}
      role="application"
      aria-label={t("Diagram canvas: drag to move, Ctrl or pinch to zoom")}
    >
      <svg className="dgx-grid" aria-hidden="true">
        <defs>
          <pattern id="dgx-dots" width={grid} height={grid} patternUnits="userSpaceOnUse"
            x={view.x % grid} y={view.y % grid}>
            <circle cx={1} cy={1} r={1} />
          </pattern>
        </defs>
        <rect width="100%" height="100%" fill="url(#dgx-dots)" />
      </svg>

      <div className="dgx-world" style={{ transform: `translate(${view.x}px, ${view.y}px) scale(${view.k})` }}>
        <svg className="dgx-edges" width={layout.width} height={layout.height} aria-hidden="true">
          {layout.nodes.flatMap((node) => node.chunk.depends_on.map((dep) => {
            const from = byKey.get(dep);
            if (!from) return null;
            const x1 = from.x + NODE_W;
            const y1 = from.y + NODE_H / 2;
            const x2 = node.x;
            const y2 = node.y + NODE_H / 2;
            const bend = Math.max(32, (x2 - x1) / 2);
            const lit = selected !== null && (selected === dep || selected === node.chunk.chunk_key);
            // An edge carries what was handed over once the upstream chunk merged.
            const handed = from.chunk.status === "merged";
            return (
              <path key={`${dep}->${node.chunk.chunk_key}`}
                d={`M${x1},${y1} C${x1 + bend},${y1} ${x2 - bend},${y2} ${x2},${y2}`}
                className={`${statusOf(node.chunk).tone === "risk" ? "risk" : ""}${lit ? " lit" : ""}${handed ? " handed" : ""}`} />
            );
          }))}
        </svg>
        {layer === "live" && layout.nodes.map((node) => {
          // One label per upstream chunk, just right of it: what its edges carry.
          const label = node.chunk.status === "merged" ? deliveredLabel(node.chunk) : null;
          if (!label || !layout.nodes.some((other) => other.chunk.depends_on.includes(node.chunk.chunk_key))) return null;
          return (
            <span key={`delivered-${node.chunk.chunk_key}`} className="dgx-delivered"
              style={{ left: node.x + NODE_W + 6, top: node.y + NODE_H / 2 + 6 }}>
              {label}
            </span>
          );
        })}
        {layout.nodes.map(({ chunk, x, y }) => {
          const status = statusOf(chunk);
          const now = layer === "live" ? live.get(chunk.chunk_key) : undefined;
          const reason = chunk.task_key ? unplaced.get(chunk.task_key) : undefined;
          const criteria = Array.isArray(chunk.done_criteria) ? chunk.done_criteria.length : 0;
          return (
            <button
              key={chunk.chunk_key}
              type="button"
              className={`dgx-node tone-${status.tone}${now ? ` live st-${now.state}` : ""}${selected === chunk.chunk_key ? " selected" : ""}`}
              style={{ left: x, top: y, width: NODE_W, height: NODE_H }}
              aria-pressed={selected === chunk.chunk_key}
              onClick={() => onSelect(selected === chunk.chunk_key ? null : chunk.chunk_key)}
              onDoubleClick={() => onAsk(chunk.chunk_key)}
            >
              <span className="dg-chunk-top">
                <span className="dg-key">{chunk.chunk_key}</span>
                {chunk.layer === "interface" && <span className="dg-tag">{t("Contract")}</span>}
                {now ? (
                  <span className="dg-state-chip">
                    {t(STATE_LABEL[now.state])}
                    {!["done", "not_ready"].includes(now.state) && now.since ? ` · ${waited(now.since, t)}` : ""}
                  </span>
                ) : (
                  <span className={`dg-status tone-${status.tone}`}>{t(status.label)}</span>
                )}
              </span>
              <strong>{chunk.title}</strong>
              {layer === "plan" ? (
                <span className="dg-chunk-foot">
                  {chunk.scope ? chunk.scope : t("No scope written")}
                  {criteria ? ` · ${t("{n} done criteria", { n: criteria })}` : ""}
                </span>
              ) : now?.holder ? (
                <span className="dg-chunk-foot">
                  <AgentFace id={now.holder} size={16} />
                  {now.holder}{now.where ? ` · ${now.where}` : ""}
                </span>
              ) : now?.state === "not_ready" && now.waiting_on?.length ? (
                <span className="dg-chunk-foot">{t("waits for {list}", { list: now.waiting_on.join(", ") })}</span>
              ) : reason ? (
                <span className="dg-chunk-foot warn">⚠ {t(UNPLACED_SHORT[reason] ?? reason)}</span>
              ) : now?.state === "done" && chunk.merge?.by ? (
                <span className="dg-chunk-foot">{t("merged by {who}", { who: chunk.merge.by })}</span>
              ) : null}
            </button>
          );
        })}
      </div>

      {layout.nodes.length === 0 && (
        <div className="dgx-hint">{t("This diagram has no chunks yet.")}</div>
      )}

      <div className="dgx-float dgx-zoom" role="group" aria-label={t("Zoom")}>
        <button type="button" aria-label={t("Zoom out")} onClick={() => zoomAt(1 / 1.2, ...center())}>−</button>
        <span>{Math.round(view.k * 100)}%</span>
        <button type="button" aria-label={t("Zoom in")} onClick={() => zoomAt(1.2, ...center())}>+</button>
        <button type="button" onClick={fit}>{t("Fit")}</button>
      </div>
    </div>
  );
}

function ChunkPanel({
  chunk,
  live,
  unplacedReason,
  onOpenTask,
  onAsk,
}: {
  chunk: Chunk;
  live?: FlowItem;
  unplacedReason?: string;
  onOpenTask: (taskKey: string) => void;
  onAsk: () => void;
}) {
  const { t } = useI18n();
  const status = statusOf(chunk);
  const criteria = Array.isArray(chunk.done_criteria)
    ? chunk.done_criteria.map((item) =>
        typeof item === "string" ? item : (item as { text?: string })?.text ?? JSON.stringify(item))
    : [];
  return (
    <div className="dgx-section">
      <h3>{chunk.title}</h3>
      {!live && <span className={`dg-status tone-${status.tone}`}>{t(status.label)}</span>}
      {live && (
        <dl className="dg-facts dg-now">
          <dt>{t("Now")}</dt>
          <dd>
            {t(STATE_LABEL[live.state])}
            {!["done", "not_ready"].includes(live.state) && live.since ? ` · ${waited(live.since, t)}` : ""}
          </dd>
          {live.holder && (
            <>
              <dt>{t("Who")}</dt>
              <dd className="dg-who"><AgentFace id={live.holder} size={18} />{live.holder}{live.where ? ` · ${live.where}` : ""}</dd>
            </>
          )}
          {live.state === "not_ready" && live.waiting_on && live.waiting_on.length > 0 && (
            <>
              <dt>{t("Waits for")}</dt>
              <dd>{live.waiting_on.join(", ")}</dd>
            </>
          )}
          {unplacedReason && (
            <>
              <dt>{t("Why it has not started")}</dt>
              <dd className="warn">{t(UNPLACED_SHORT[unplacedReason] ?? unplacedReason)}</dd>
            </>
          )}
          {deliveredLabel(chunk) && (
            <>
              <dt>{t("Handed over")}</dt>
              <dd>{deliveredLabel(chunk)}</dd>
            </>
          )}
        </dl>
      )}
      {chunk.scope && <p className="dgx-text">{chunk.scope}</p>}
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
      <div className="dg-panel-actions">
        <button type="button" className="button ghost small" onClick={onAsk}>{t("Ask for a change here")}</button>
        {chunk.task_key && (
          <button type="button" className="button ghost small" onClick={() => onOpenTask(chunk.task_key!)}>
            {t("Open its task")}
          </button>
        )}
      </div>
    </div>
  );
}

/**
 * Every plan in one place: the diagrams, then work that is planned but not
 * drawn as a diagram yet (a task with subtasks, a family of tasks, the inbox).
 */
function Portfolio({
  diagrams,
  lanes,
  onOpenDiagram,
  onOpenLoop,
}: {
  diagrams: DiagramSummary[];
  lanes: Lane[];
  onOpenDiagram: (key: string) => void;
  onOpenLoop: () => void;
}) {
  const { t } = useI18n();
  const byDiagram = new Map(lanes.filter((lane) => lane.kind === "diagram").map((lane) => [lane.key, lane]));
  const others = lanes.filter((lane) => lane.kind !== "diagram");
  const card = (lane: Lane | undefined) => {
    if (!lane) return null;
    const states = (Object.keys(lane.counts) as FlowItem["state"][]).filter((state) => lane.counts[state]);
    return (
      <>
        <span className="flow-stack" aria-hidden="true">
          {states.map((state) => <i key={state} className={`st-${state}`} style={{ flexGrow: lane.counts[state] }} />)}
        </span>
        <small>
          {t("{done} of {total} done", { done: lane.counts.done, total: lane.total })}
          {lane.stuck ? ` · ${t(STATE_LABEL[lane.stuck.state])} ${waited(lane.stuck.since, t)}: ${lane.stuck.title}` : ""}
        </small>
      </>
    );
  };
  return (
    <section className="dgx-portfolio">
      <h3>{t("Diagrams")}</h3>
      <div className="dgx-cards">
        {diagrams.map((item) => (
          <button key={item.diagram_key} type="button" className="dgx-card" onClick={() => onOpenDiagram(item.diagram_key)}>
            <b>{item.title}</b>
            <span className="dgx-meta">{t(DIAGRAM_STATUS[item.status])} · v{item.version}</span>
            {card(byDiagram.get(item.diagram_key))}
          </button>
        ))}
      </div>
      {others.length > 0 && (
        <>
          <h3>{t("Planned, not drawn as a diagram yet")}</h3>
          <div className="dgx-cards">
            {others.map((lane) => (
              <button key={lane.lane_id} type="button" className="dgx-card virtual" onClick={onOpenLoop}>
                <b>{lane.kind === "inbox" ? t("Inbox") : lane.title ?? lane.key}</b>
                <span className="dgx-meta">
                  {t(lane.kind === "umbrella" ? "Task with subtasks" : lane.kind === "family" ? "Family of tasks" : "Tasks outside a plan")}
                </span>
                {card(lane)}
              </button>
            ))}
          </div>
        </>
      )}
    </section>
  );
}
