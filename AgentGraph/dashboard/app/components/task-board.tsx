"use client";

import { useEffect, useState, type ReactNode } from "react";

import { useI18n } from "../i18n";
import type {
  ApiRequest,
  DirectoryEntry,
  ExecutionView,
  OpenQuestion,
  TaskDetail,
  TaskSummary,
} from "../types";
import { directoryDisplayName, directoryRefs, formatNumber, pretty, relativeTime } from "../types";
import {
  Empty,
  IconCheck,
  IconChevron,
  IconLock,
  IconPlus,
  IconX,
  Modal,
  Spinner,
  StatusPill,
} from "./ui";
import { MarkdownContent } from "./markdown";

// Tasks read as one list, grouped by what they need: people first.
const GROUPS: Array<{ title: string; statuses: string[]; lamp: string; always?: boolean }> = [
  { title: "Needs you", statuses: ["AWAITING_HUMAN", "ESCALATED_TO_HUMAN", "WAITING_HUMAN"], lamp: "red" },
  { title: "In review", statuses: ["REVIEWING", "HANDOFF"], lamp: "cyan" },
  { title: "In progress", statuses: ["IN_PROGRESS"], lamp: "amber", always: true },
  { title: "Ready", statuses: ["TODO", "READY"], lamp: "", always: true },
  // CANCELLED and WAITING_HUMAN once had no column, so those tasks silently vanished.
  { title: "Closed", statuses: ["DONE", "FAILED", "CANCELLED"], lamp: "green" },
];

const NOISE_KEY = "veynod.board.hide_noise";

// Board noise (2026-10-24 playtest): failed or cancelled work and probe/test tasks.
export function isNoise(task: TaskSummary): boolean {
  if (["FAILED", "CANCELLED"].includes(String(task.status))) return true;
  return /(^|[-_/: ])(probe|playtest|smoke|ux-probe)([-_/: ]|$)/i.test(`${task.task_key} ${task.title}`);
}

export function TaskBoard({
  tasks: allTasks,
  directory = [],
  onOpen,
  onCreate,
}: {
  tasks: TaskSummary[];
  directory?: DirectoryEntry[];
  onOpen: (task: TaskSummary) => void;
  onCreate: () => void;
}) {
  const { t } = useI18n();
  const [hideNoise, setHideNoise] = useState(true);
  useEffect(() => {
    try {
      setHideNoise(localStorage.getItem(NOISE_KEY) !== "0");
    } catch {
      // Private mode: keep the default.
    }
  }, []);
  function toggleNoise() {
    setHideNoise((value) => {
      try {
        localStorage.setItem(NOISE_KEY, value ? "0" : "1");
      } catch {
        // ignore
      }
      return !value;
    });
  }
  const hidden = allTasks.filter(isNoise).length;
  const tasks = hideNoise ? allTasks.filter((task) => !isNoise(task)) : allTasks;
  const known = directoryRefs(directory);
  const unregistered = (task: TaskSummary) =>
    Boolean(task.assignee?.startsWith("agent:") && directory.length > 0 && !known.has(task.assignee));
  return (
    <div className="tab-stack">
      <div className="board-toolbar">
        <div>
          <h2 className="section-title">{t("Tasks")}</h2>
        </div>
        <div className="row-actions">
          {hidden > 0 && (
            <button type="button" className="button ghost small" onClick={toggleNoise}
              title={t("Failed or cancelled tasks, and probe / playtest / smoke tasks")}>
              {hideNoise
                ? t("Show {count} hidden", { count: hidden })
                : t("Hide failed and probe tasks")}
            </button>
          )}
          <button
            type="button"
            className="button primary"
            onClick={onCreate}
            data-modal-trigger="new-task"
          >
            <IconPlus /> {t("New task")}
          </button>
        </div>
      </div>
      {allTasks.length === 0 && (
        <Empty
          title={t("No tasks yet")}
          detail={t("Create a task for your agents, or let an ANALYST agent plan one. Agents claim READY tasks under a lease and hand them back for your review.")}
          action={
            <div className="empty-actions">
              <button type="button" className="button primary small" onClick={onCreate}>
                <IconPlus /> {t("New task")}
              </button>
              <a className="button ghost small" href="/docs/workflows/" target="_blank" rel="noreferrer">
                {t("How tasks work")}
              </a>
            </div>
          }
        />
      )}
      <section className="lin-list" aria-label={t("Tasks")}>
        {GROUPS.map((group) => {
          const items = tasks.filter((task) => group.statuses.includes(task.status));
          if (items.length === 0 && !group.always) return null;
          return (
            <div key={group.title} role="group" aria-label={t(group.title)}>
              <div className="lin-group">
                <i className={`lamp ${group.lamp}`} />
                {t(group.title)}
                <span className="lin-count">{items.length}</span>
              </div>
              {items.length === 0 && (
                <div className="lin-row quiet"><span className="lin-title">{t("No tasks")}</span></div>
              )}
              {items.map((task) => (
                <button
                  key={task.id}
                  type="button"
                  className={`lin-row task-row task-status-${task.status}`}
                  onClick={() => onOpen(task)}
                >
                  <i className={`lamp ${group.lamp}`} />
                  <span className="lin-key" title={task.task_key}>{task.task_key}</span>
                  <span className="lin-title" title={task.title}>{task.title}</span>
                  <span className="lin-pills">
                    {task.priority > 0 && <span className="priority-chip">P{task.priority}</span>}
                    <StatusPill status={task.status} />
                    {task.tainted && <span className="pill rose" title={t("Task input references tainted content")}>{t("Tainted")}</span>}
                    {unregistered(task) && (
                      <span className="pill amber" title={t("{who} is not in the agent directory: it may never see this task", { who: directoryDisplayName(directory, task.assignee) })}>
                        {t("Assignee not registered")}
                      </span>
                    )}
                    {task.ping_pong_count > 0 && (
                      <span className="pp-chip" title={t("Review ping-pong count")}>
                        {task.ping_pong_count}/{task.max_retries}
                      </span>
                    )}
                  </span>
                  <span className="lin-meta">
                    {task.claimed_by
                      ? directoryDisplayName(directory, task.claimed_by)
                      : task.status === "READY" ? t("unclaimed") : ""}
                  </span>
                  <span className="lin-meta lin-time">{relativeTime(task.updated_at)}</span>
                </button>
              ))}
            </div>
          );
        })}
      </section>
    </div>
  );
}

type Attribution = {
  project?: string;
  projectUrl?: string;
  repo?: string;
  localPath?: string;
  deployTarget?: string;
  docKey?: string;
  supersedes?: string;
};

/** Structured fields win; older tasks fall back to the 【项目归属】 section. */
function parseAttribution(task: TaskDetail): Attribution {
  const out: Attribution = {};
  if (task.project) out.project = task.project;
  if (task.repo) out.repo = task.repo;
  if (task.supersedes) out.supersedes = task.supersedes;
  const text = task.description ?? "";
  const lineValue = (pattern: RegExp) => text.match(pattern)?.[1]?.trim();
  if (!out.project) {
    const value = lineValue(/(?:产品|项目)[：:]\s*([^\n]+)/);
    if (value) {
      const url = value.match(/[（(](https?:\/\/[^）)]+)[）)]/);
      out.project = value.replace(/[（(].*$/, "").trim();
      if (url) out.projectUrl = url[1];
    }
  }
  if (!out.repo) {
    out.repo = lineValue(/(?:GitHub|仓库)[：:]\s*([A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+)/);
  }
  out.localPath = lineValue(/本地路径[：:]\s*([^\n]+)/);
  out.deployTarget = lineValue(/部署目标[：:]\s*([^\n]+)/);
  if (!out.docKey) {
    out.docKey = text.match(/doc:\/\/([a-z0-9-]+)/)?.[1];
  }
  if (!out.supersedes) {
    const old = text.match(/替代旧单[（(]([^）)]+)[）)]/)
      ?? text.match(/替代[：:]\s*([A-Za-z0-9_./-]+)/);
    if (old) out.supersedes = old[1].trim();
  }
  return out;
}

function isPendingValue(value?: string): boolean {
  return Boolean(value && /待确认|待 owner|待定|PENDING/i.test(value));
}

function hasMarkdown(text: string): boolean {
  return /(^|\n)\s{0,3}(#{1,6}\s|[-*+]\s|\d+\.\s|>\s|\|)|```|`[^`]+`|\*\*|\[[^\]]+\]\(/.test(text);
}

/** Turn bare doc:// references into links; inline code is left untouched. */
function linkDocRefs(text: string, docTitle?: (key: string) => string | undefined): string {
  return text
    .split(/(`[^`]*`)/g)
    .map((part, index) => {
      if (index % 2 === 1) return part;
      return part.replace(/doc:\/\/([a-z0-9-]+)/g, (match, key: string) => {
        const title = docTitle?.(key);
        return `[${title ?? match}](${match})`;
      });
    })
    .join("");
}

const ACCEPTANCE_TONE: Record<string, string> = {
  PASS: "mint",
  FAIL: "rose",
  PENDING: "slate",
};

const ACCEPTANCE_LABEL: Record<string, string> = {
  PASS: "Passed",
  FAIL: "Failed",
  PENDING: "Pending",
};

function TaskFields({
  task,
  onOpenTask,
  onOpenDoc,
  docTitle,
}: {
  task: TaskDetail;
  onOpenTask?: (taskKey: string) => void;
  onOpenDoc?: (docKey: string) => void;
  docTitle?: (docKey: string) => string | undefined;
}) {
  const { t } = useI18n();
  const attribution = parseAttribution(task);
  const fields: ReactNode[] = [];
  if (attribution.project) {
    fields.push(
      <span key="project" className="task-field">
        <span>{t("Project")}</span>
        {attribution.projectUrl ? (
          <a href={attribution.projectUrl} target="_blank" rel="noopener noreferrer">
            {attribution.project}
          </a>
        ) : (
          <strong>{attribution.project}</strong>
        )}
      </span>,
    );
  }
  if (attribution.repo) {
    const repo = attribution.repo;
    const repoUrl = /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(repo)
      ? `https://github.com/${repo}`
      : undefined;
    fields.push(
      <span key="repo" className="task-field">
        <span>{t("Repo")}</span>
        {repoUrl ? (
          <a href={repoUrl} target="_blank" rel="noopener noreferrer" className="mono">
            {repo}
          </a>
        ) : (
          <strong className="mono">{repo}</strong>
        )}
      </span>,
    );
  }
  if (attribution.supersedes) {
    fields.push(
      <span key="supersedes" className="task-field">
        <span>{t("Supersedes")}</span>
        {onOpenTask ? (
          <button
            type="button"
            className="question-link mono"
            onClick={() => onOpenTask(attribution.supersedes!)}
          >
            {attribution.supersedes}
          </button>
        ) : (
          <span className="mono">{attribution.supersedes}</span>
        )}
      </span>,
    );
  }
  if (attribution.docKey) {
    fields.push(
      <span key="doc" className="task-field">
        <span>{t("Requirement doc")}</span>
        <button
          type="button"
          className="link-button"
          disabled={!onOpenDoc}
          onClick={() => attribution.docKey && onOpenDoc?.(attribution.docKey)}
        >
          {docTitle?.(attribution.docKey) ?? attribution.docKey}
        </button>
      </span>,
    );
  }
  const pairs: Array<[string, string | undefined]> = [
    ["Local path", attribution.localPath],
    ["Deploy target", attribution.deployTarget],
  ];
  for (const [label, value] of pairs) {
    if (!value) continue;
    fields.push(
      <span key={label} className="task-field">
        <span>{t(label)}</span>
        {isPendingValue(value) ? (
          <span className="pending-badge">{value}</span>
        ) : (
          <span className="mono">{value}</span>
        )}
      </span>,
    );
  }
  if (fields.length === 0) return null;
  return <div className="task-fields">{fields}</div>;
}

function AcceptanceSection({ task }: { task: TaskDetail }) {
  const { t } = useI18n();
  const items = task.acceptance ?? [];
  if (items.length === 0) return null;
  const states = new Map((task.acceptance_state ?? []).map((entry) => [entry.id, entry.status]));
  return (
    <div className="detail-section acceptance-section">
      <div className="detail-section-head">
        <h3>{t("Acceptance")}</h3>
        {task.acceptance_locked && (
          <span className="lock-chip">
            <IconLock size={11} /> {t("Locked after first claim")}
          </span>
        )}
      </div>
      <ol className="acceptance-list">
        {items.map((item, index) => {
          const id = item.id ?? item.item_id ?? `item-${index}`;
          const status = states.get(id) ?? "PENDING";
          return (
            <li key={id} className="acceptance-item">
              <span className={`status-pill acceptance-status tone-${ACCEPTANCE_TONE[status] ?? "slate"}`}>
                {t(ACCEPTANCE_LABEL[status] ?? "Pending")}
              </span>
              <span className="acceptance-id mono">{id}</span>
              <span className="acceptance-kind">{item.kind === "AUTO" ? "AUTO" : "MANUAL"}</span>
              <div className="acceptance-text">{item.statement ?? item.text ?? ""}</div>
            </li>
          );
        })}
      </ol>
    </div>
  );
}

function KeyValueSection({
  title,
  value,
}: {
  title: string;
  value: Record<string, unknown>;
}) {
  const { t } = useI18n();
  const [raw, setRaw] = useState(false);
  const entries = Object.entries(value ?? {});
  return (
    <details className="detail-section kv-block" open={entries.length <= 6}>
      <summary className="kv-summary">
        <h3>{title}</h3>
        <span className="muted">{entries.length}</span>
        <button
          type="button"
          className="button ghost small"
          onClick={(event) => {
            event.preventDefault();
            setRaw((current) => !current);
          }}
        >
          {raw ? t("Key-value") : t("Raw JSON")}
        </button>
      </summary>
      {raw ? (
        <pre className="json-block">{pretty(value)}</pre>
      ) : entries.length > 0 ? (
        <table className="kv-table">
          <tbody>
            {entries.map(([key, item]) => (
              <tr key={key}>
                <th>{key}</th>
                <td>
                  {item !== null && typeof item === "object" ? (
                    <code>{JSON.stringify(item)}</code>
                  ) : (
                    String(item ?? "—")
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <p className="muted">{t("Empty")}</p>
      )}
    </details>
  );
}

/**
 * One task's execution, from the three owners: the Agent Runtime loop, the
 * Kernel's code facts and its durable gate evidence.  This replaces the three
 * separate technical consoles (ADR-018): Code / Agent Runs / Delivery are
 * sections of a task, not products.
 */
export function ExecutionPanel({ api, taskKey }: { api: ApiRequest; taskKey: string }) {
  const { t } = useI18n();
  const [view, setView] = useState<ExecutionView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState(true);

  useEffect(() => {
    let cancelled = false;
    setView(null);
    setError(null);
    const load = async () => {
      try {
        const payload = await api<ExecutionView>(
          `/v1/tasks/${encodeURIComponent(taskKey)}/execution`,
        );
        if (!cancelled) setView(payload);
      } catch (exc) {
        if (!cancelled) setError(exc instanceof Error ? exc.message : String(exc));
      }
    };
    void load();
    // Gate and merge land after the agent exits. Refresh while this panel is open.
    const timer = window.setInterval(() => { void load(); }, 4000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [api, taskKey]);

  if (error) {
    return (
      <div className="detail-section">
        <h3>{t("Execution")}</h3>
        <p className="muted">{t("No execution yet: {reason}", { reason: error })}</p>
      </div>
    );
  }
  if (!view) {
    return (
      <div className="detail-section">
        <h3>{t("Execution")}</h3>
        <p className="muted"><Spinner /> {t("Loading the loop…")}</p>
      </div>
    );
  }
  if (!view.loop && !view.code && !view.kernel) {
    return null;
  }
  const loop = view.loop;
  const steps = loop?.steps ?? [];
  return (
    <div className="detail-section execution-panel">
      <h3>
        <button
          type="button"
          className="button ghost small"
          onClick={() => setOpen((value) => !value)}
          aria-expanded={open}
        >
          <IconChevron />
          {t("Execution")}
        </button>
        {loop?.status && <StatusPill status={loop.status} />}
        {loop?.waiting_on && (
          <span className="muted"> {t("waiting on {thing}", { thing: loop.waiting_on })}</span>
        )}
      </h3>
      {open && (
        <>
          {loop && (
            <div className="execution-loop">
              <div className="muted">
                {t("Run {run} · {steps} step(s)", {
                  run: String(loop.run_id ?? "").slice(0, 8),
                  steps: formatNumber(loop.step_count ?? steps.length),
                })}
              </div>
              <ol className="lin-list execution-steps">
                {steps
                  .filter((step) => step.kind === "TOOL" || step.kind === "MODEL")
                  .slice(-8)
                  .map((step, index) => {
                    const payload = (step.payload ?? {}) as Record<string, unknown>;
                    const tool = String(payload.tool ?? step.kind ?? "");
                    return (
                      <li key={`${step.step_no ?? index}-${tool}`}>
                        <span
                          className={`status-pill tone-${
                            step.status === "FAILED" ? "rose" : "mint"
                          }`}
                        >
                          {String(step.status ?? "")}
                        </span>{" "}
                        <span className="mono">{tool}</span>
                        {step.created_at && (
                          <span className="muted"> · {relativeTime(step.created_at)}</span>
                        )}
                      </li>
                    );
                  })}
              </ol>
              {loop.last_error && <p className="muted">{loop.last_error}</p>}
            </div>
          )}
          {view.kernel && (
            <div className="execution-code">
              <div>
                <span className="muted">{t("Worktree")}: </span>
                <span className="mono">{view.kernel.worktree_path || view.kernel.worktree || "idle"}</span>
              </div>
              <div>
                <span className="muted">{t("Lease")}: </span>
                {view.kernel.lease || "none"}
                {view.kernel.gate ? ` · ${t("Gate")} ${view.kernel.gate}` : ""}
              </div>
              {view.kernel.merge_sha && (
                <div>
                  <span className="muted">{t("Merge")}: </span>
                  <span className="mono">{view.kernel.merge_sha.slice(0, 12)}</span>
                </div>
              )}
            </div>
          )}
          {view.code && (
            <div className="execution-code">
              <div>
                <span className="muted">{t("Kernel task")}: </span>
                <span className="mono">{view.code.kernel_task_id}</span>
              </div>
              {view.code.submission_status && (
                <div>
                  <span className="muted">{t("Submission")}: </span>
                  {view.code.submission_status}
                </div>
              )}
              {view.code.commit_sha && (
                <div>
                  <span className="muted">{t("Commit")}: </span>
                  <span className="mono">{view.code.commit_sha.slice(0, 12)}</span>
                </div>
              )}
            </div>
          )}
          {view.verification && (view.verification.gate_results ?? []).length > 0 && (
            <div className="execution-gates">
              {(view.verification.gate_results ?? []).map((gate, index) => (
                <div key={index} className="gate-row">
                  <span
                    className={`status-pill tone-${
                      gate.passed === true ? "mint" : "rose"
                    }`}
                  >
                    {gate.passed === true ? "PASS" : "FAIL"}
                  </span>{" "}
                  <span className="mono">{String(gate.stage ?? "")}</span>
                </div>
              ))}
            </div>
          )}
          {view.delivery && (
            <div className="execution-delivery">
              <span className="muted">{t("Delivered")}: </span>
              <span className="mono">
                {String((view.delivery as Record<string, unknown>).commit_sha ?? "").slice(0, 12)}
              </span>
            </div>
          )}
        </>
      )}
    </div>
  );
}

export function TaskDetailModal({
  task,
  directory = [],
  api,
  busy,
  onClose,
  onReview,
  onOpenArtifact,
  onOpenTask,
  docTitle,
  question = null,
  onAnswer,
}: {
  task: TaskDetail;
  directory?: DirectoryEntry[];
  api: ApiRequest;
  busy: boolean;
  onClose: () => void;
  onReview: (decision: "approve" | "reject", feedback: string) => Promise<void>;
  question?: OpenQuestion | null;
  onAnswer?: (optionId: string | null, answer: string) => Promise<void>;
  onOpenArtifact: (artifactUri: string) => void;
  onOpenTask?: (taskKey: string) => void;
  docTitle?: (docKey: string) => string | undefined;
}) {
  const { t } = useI18n();
  const [feedback, setFeedback] = useState(task.human_feedback ?? "");
  useEffect(() => {
    setFeedback(task.human_feedback ?? "");
  }, [task.id, task.human_feedback]);

  // A question an agent asked is answered, not approved: the answer box replaces
  // approve/reject on the question task too.
  const answering = Boolean(question && onAnswer);
  const reviewable = !answering && ["REVIEWING", "AWAITING_HUMAN", "ESCALATED_TO_HUMAN"].includes(
    task.status,
  );
  const [choice, setChoice] = useState<string | null>(question?.recommended_option ?? null);
  const [answer, setAnswer] = useState("");
  useEffect(() => {
    setChoice(question?.recommended_option ?? null);
    setAnswer("");
  }, [question?.id, question?.recommended_option]);
  const openDoc = (docKey: string) => onOpenArtifact(`doc://${docKey}`);

  return (
    <Modal
      title={
        <span className="modal-title-row">
          <span className="mono">{task.task_key}</span>
          <StatusPill status={task.status} />
        </span>
      }
      subtitle={task.title}
      onClose={onClose}
      wide
      footer={
        <>
          <button type="button" className="button ghost" onClick={onClose}>
            {t("Close")}
          </button>
          {answering && (
            <button
              type="button"
              className="button primary"
              disabled={busy || (!choice && !answer.trim())}
              title={choice || answer.trim() ? undefined : t("Pick an option or write an answer.")}
              onClick={() => void onAnswer!(choice, answer)}
            >
              {busy ? <Spinner /> : <IconCheck />} {t("Send answer")}
            </button>
          )}
          {reviewable && (
            <>
              <button
                type="button"
                className="button danger"
                disabled={busy || !feedback.trim()}
                title={feedback.trim() ? undefined : t("A rejection needs a reason: write feedback first.")}
                onClick={() => void onReview("reject", feedback)}
              >
                {busy ? <Spinner /> : <IconX />} {t("Reject")}
              </button>
              <button
                type="button"
                className="button primary"
                disabled={busy}
                onClick={() => void onReview("approve", feedback)}
              >
                {busy ? <Spinner /> : <IconCheck />} {t("Approve")}
              </button>
            </>
          )}
        </>
      }
    >
      <TaskFields task={task} onOpenTask={onOpenTask} onOpenDoc={openDoc} docTitle={docTitle} />

      <ExecutionPanel api={api} taskKey={task.task_key} />

      {question && answering && (
        <div className="detail-section question-section">
          <h3>
            {question.raised_by
              ? t("{who} asks you", { who: directoryDisplayName(directory, question.raised_by) })
              : t("An agent asks you")}
            <span className="muted"> · {relativeTime(question.created_at)}</span>
          </h3>
          <div className="question-box">
            <p className="question-text">{question.question}</p>
            {(question.options ?? []).length > 0 && (
              <div className="question-options" role="radiogroup" aria-label={t("Options")}>
                {question.options!.map((option) => (
                  <label key={option.id} className={choice === option.id ? "picked" : undefined}>
                    <input
                      type="radio"
                      name={`question-${question.id}`}
                      checked={choice === option.id}
                      onChange={() => setChoice(option.id)}
                    />
                    <span>
                      <b>{option.label}</b>
                      {option.id === question.recommended_option && (
                        <em className="question-recommended">{t("recommended")}</em>
                      )}
                      {option.consequence && <small>{option.consequence}</small>}
                    </span>
                  </label>
                ))}
                {choice && (
                  <button type="button" className="button ghost small" onClick={() => setChoice(null)}>
                    {t("Answer in words instead")}
                  </button>
                )}
              </div>
            )}
            <label className="field">
              <span>{(question.options ?? []).length > 0 ? t("Note (optional)") : t("Your answer")}</span>
              <textarea
                rows={3}
                value={answer}
                onChange={(event) => setAnswer(event.target.value)}
                placeholder={t("What the agent should know or do…")}
              />
            </label>
            {question.due_at && (
              <p className="question-meta">
                {t("If nobody answers by {when}, it goes with {option}.", {
                  when: new Date(question.due_at).toLocaleString(),
                  option: question.default_on_timeout ?? "—",
                })}
              </p>
            )}
            {question.escalation_task_key && question.escalation_task_key !== task.task_key && (
              <p className="question-meta">{t("Asked in")} <span className="mono">{question.escalation_task_key}</span></p>
            )}
            {question.task_key && question.task_key !== task.task_key && (
              <p className="question-meta">
                {t("It blocks")}{" "}
                {onOpenTask ? (
                  <button type="button" className="question-link mono" onClick={() => onOpenTask(question.task_key!)}>
                    {question.task_key}
                  </button>
                ) : <span className="mono">{question.task_key}</span>}
              </p>
            )}
          </div>
        </div>
      )}

      <AcceptanceSection task={task} />

      {task.description && (
        <div className="detail-section description-section">
          <h3>{t("Description")}</h3>
          {hasMarkdown(task.description) ? (
            <MarkdownContent
              content={linkDocRefs(task.description, docTitle)}
              onDocLink={openDoc}
              docTitle={docTitle}
            />
          ) : (
            <p className="detail-description">{task.description}</p>
          )}
        </div>
      )}

      <div className="detail-grid">
        <div className="detail-cell">
          <span>{t("Claim / lease")}</span>
          <strong>
            {task.claimed_by ? directoryDisplayName(directory, task.claimed_by) : t("unclaimed")}
            {task.lease_active ? ` · ${t("live")}` : ""}
          </strong>
        </div>
        <div className="detail-cell">
          <span>{t("Assignee")}</span>
          <strong>
            {task.assignee ? directoryDisplayName(directory, task.assignee) : t("Anyone")}
          </strong>
        </div>
        <div className="detail-cell">
          <span>{t("Assigned role")}</span>
          <strong>{task.assigned_role ? t(task.assigned_role) : t("Any role")}</strong>
        </div>
        <div className="detail-cell">
          <span>{t("Priority")}</span>
          <strong>{task.priority > 0 ? `P${task.priority}` : t("Normal")}</strong>
        </div>
        <div className="detail-cell">
          <span>{t("Namespace")}</span>
          <strong>{task.namespace}</strong>
        </div>
      </div>

      {task.review_feedback && (
        <div className="detail-section">
          <h3>{t("Review feedback")}</h3>
          <div className="brief-box">
            <div className="brief-summary">{task.review_feedback}</div>
            {task.reviewed_by && (
              <div className="brief-metrics">
                <span className="metric-chip">{t("Reviewer")}: <strong>{task.reviewed_by}</strong></span>
              </div>
            )}
          </div>
        </div>
      )}

      <div className="detail-section">
        <div className="detail-section-head">
          <h3>{t("L1 handoff brief")}</h3>
          {task.artifact_uri && (
            <button
              type="button"
              className="button ghost small"
              onClick={() => onOpenArtifact(task.artifact_uri!)}
            >
              {t("Open L2 artifact")} <IconChevron size={13} />
            </button>
          )}
        </div>
        {task.handoff_brief ? (
          <div className="brief-box">
            <div className="brief-summary">{String(task.handoff_brief.summary ?? "—")}</div>
            <div className="brief-metrics">
              {Object.entries(task.handoff_brief.key_metrics ?? {}).map(([key, value]) => (
                <span key={key} className="metric-chip">
                  {key}: <strong>{String(value)}</strong>
                </span>
              ))}
              {task.handoff_brief.risk_flag && (
                <span className="metric-chip risk">
                  <IconLock size={11} /> {t("risk flagged")}
                </span>
              )}
            </div>
            {(task.handoff_brief.unverified_hypotheses ?? []).length > 0 && (
              <ul className="hypothesis-list">
                {task.handoff_brief.unverified_hypotheses!.map((item, index) => (
                  <li key={index}>{item}</li>
                ))}
              </ul>
            )}
          </div>
        ) : (
          <p className="muted">{t("No L1 brief yet.")}</p>
        )}
      </div>

      <div className="detail-columns">
        <KeyValueSection title={t("Input context")} value={task.input_context ?? {}} />
        <KeyValueSection title={t("Output artifact")} value={task.output_artifact ?? {}} />
      </div>

      {(task.reject_history ?? []).length > 0 && (
        <div className="detail-section">
          <h3>{t("Reject history")}</h3>
          <pre className="json-block">{pretty(task.reject_history)}</pre>
        </div>
      )}

      {reviewable && (
        <label className="field">
          <span>{t("Human feedback")}</span>
          <textarea
            rows={3}
            value={feedback}
            onChange={(event) => setFeedback(event.target.value)}
            placeholder={t("Approval note or rejection reason…")}
          />
        </label>
      )}
      <div className="detail-footer-meta">
        {t("Version")} v{task.version} · {t("Ping-pong")} {task.ping_pong_count} /{" "}
        {task.max_retries}
        {task.review_round ? ` · ${t("Review round")} ${task.review_round}` : ""}
        {" · "}
        {t("Created by")} {task.created_by} · {relativeTime(task.created_at)} ·{" "}
        {t("Updated")} {relativeTime(task.updated_at)}
      </div>
    </Modal>
  );
}

export function CreateTaskModal({
  busy,
  onClose,
  onCreate,
}: {
  busy: boolean;
  onClose: () => void;
  onCreate: (payload: Record<string, unknown>) => Promise<void>;
}) {
  const { t } = useI18n();
  const [key, setKey] = useState("");
  const [title, setTitle] = useState("");
  const [role, setRole] = useState("EXECUTOR");
  const [priority, setPriority] = useState(0);
  const [namespace, setNamespace] = useState("");
  const [project, setProject] = useState("");
  const [repo, setRepo] = useState("");
  const [description, setDescription] = useState("");
  const [context, setContext] = useState("{}");
  const [error, setError] = useState<string | null>(null);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setError(null);
    try {
      const inputContext = JSON.parse(context || "{}");
      if (!inputContext || typeof inputContext !== "object" || Array.isArray(inputContext)) {
        throw new Error(t("Input context must be a JSON object"));
      }
      await onCreate({
        task_key: key.trim() || undefined,
        title: title.trim(),
        assigned_role: role,
        priority,
        namespace: namespace.trim() || undefined,
        project: project.trim() || undefined,
        repo: repo.trim() || undefined,
        description: description.trim() || undefined,
        input_context: inputContext,
      });
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : t("Invalid task"));
    }
  }

  return (
    <Modal
      title={t("New task")}
      subtitle={t("Create a task for an agent role. Agents can also create next tasks during handoff.")}
      onClose={onClose}
      returnFocusSelector='[data-modal-trigger="new-task"]'
    >
      <form onSubmit={submit}>
        <div className="form-grid">
          <label className="field">
            <span>{t("Task key (optional)")}</span>
            <input value={key} onChange={(e) => setKey(e.target.value)} placeholder="TASK-104" />
          </label>
          <label className="field">
            <span>{t("Priority")}</span>
            <input
              type="number"
              min={-100}
              max={100}
              value={priority}
              onChange={(e) => setPriority(Number(e.target.value))}
            />
          </label>
        </div>
        <label className="field">
          <span>{t("Title")}</span>
          <input
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            placeholder={t("Validate BTC order-block liquidity")}
            required
            autoFocus
          />
        </label>
        <div className="form-grid">
          <label className="field">
            <span>{t("Assigned role")}</span>
            <select value={role} onChange={(e) => setRole(e.target.value)}>
              <option value="EXECUTOR">{t("Executor")}</option>
              <option value="ANALYST">{t("Analyst")}</option>
              <option value="AUDITOR">{t("Auditor")}</option>
              <option value="SCOUT">{t("Scout")}</option>
            </select>
          </label>
          <label className="field">
            <span>{t("Namespace")}</span>
            <input value={namespace} onChange={(e) => setNamespace(e.target.value)}
              placeholder={t("Automatic")} />
          </label>
        </div>
        <div className="form-grid">
          <label className="field">
            <span>{t("Project (optional)")}</span>
            <input value={project} onChange={(e) => setProject(e.target.value)}
              placeholder="Veynod" maxLength={128} />
          </label>
          <label className="field">
            <span>{t("Repository (optional)")}</span>
            <input value={repo} onChange={(e) => setRepo(e.target.value)}
              placeholder="MuadibUsul/veynod" maxLength={256} />
          </label>
        </div>
        <label className="field">
          <span>{t("Description")}</span>
          <textarea
            rows={3}
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder={t("What should the agent do? Point to slot/doc keys instead of pasting raw logs.")}
          />
        </label>
        <label className="field">
          <span>{t("Input context (JSON)")}</span>
          <textarea
            rows={4}
            className="mono-input"
            value={context}
            onChange={(e) => setContext(e.target.value)}
          />
        </label>
        {error && <div className="inline-error">{error}</div>}
        <div className="modal-actions">
          <button type="button" className="button ghost" onClick={onClose}>
            {t("Cancel")}
          </button>
          <button type="submit" className="button primary" disabled={busy || !title.trim()}>
            {busy ? <Spinner /> : <IconPlus />} {t("Create task")}
          </button>
        </div>
      </form>
    </Modal>
  );
}

export function TaskCount({ tasks }: { tasks: TaskSummary[] }) {
  const approvals = tasks.filter((t) => ["AWAITING_HUMAN", "ESCALATED_TO_HUMAN"].includes(t.status));
  return <>{formatNumber(approvals.length)}</>;
}
