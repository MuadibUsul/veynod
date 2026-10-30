"use client";

// The archive: finished, failed and cancelled tasks, out of the Loop's way,
// searchable by time, agent and keyword (live blackboard M-10).

import { useCallback, useEffect, useState } from "react";

import { useI18n } from "../i18n";
import { StatusPill, relativeTime } from "./ui";

const RESOLVED_LIMIT = 10;

type ArchivedCard = {
  task_key: string;
  title: string;
  status: string;
  owner: string | null;
  updated_at: string | null;
};

export function ArchivePanel({
  boardPath,
  fetcher,
  onClose,
  onOpen,
}: {
  boardPath: string;
  fetcher: <T>(path: string) => Promise<T>;
  onClose: () => void;
  onOpen: (taskKey: string) => void;
}) {
  const { t } = useI18n();
  const [q, setQ] = useState("");
  const [agent, setAgent] = useState("");
  const [since, setSince] = useState("");
  const [until, setUntil] = useState("");
  const [items, setItems] = useState<ArchivedCard[]>([]);
  const [next, setNext] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const search = useCallback(
    async (cursor: string | null) => {
      setBusy(true);
      setError(null);
      const params = new URLSearchParams({ resolved_limit: String(RESOLVED_LIMIT), limit: "50" });
      if (q.trim()) params.set("q", q.trim());
      if (agent.trim()) params.set("agent", agent.trim());
      if (since) params.set("since", new Date(since).toISOString());
      if (until) params.set("until", new Date(until).toISOString());
      if (cursor) params.set("cursor", cursor);
      try {
        const page = await fetcher<{ items: ArchivedCard[]; next_cursor: string | null }>(`${boardPath}/archive?${params}`);
        setItems((current) => (cursor ? [...current, ...page.items] : page.items));
        setNext(page.next_cursor);
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err));
      } finally {
        setBusy(false);
      }
    },
    [agent, boardPath, fetcher, q, since, until],
  );

  useEffect(() => {
    void search(null);
    // first page on open only
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div className="live-drawer live-archive" role="dialog" aria-label={t("Archive")}>
      <div className="live-drawer-head">
        <h3>{t("Archive")}</h3>
        <button type="button" className="button ghost small" onClick={onClose}>{t("Close")}</button>
      </div>
      <form
        className="live-archive-form"
        onSubmit={(event) => {
          event.preventDefault();
          void search(null);
        }}
      >
        <input value={q} onChange={(event) => setQ(event.target.value)} placeholder={t("Keyword")} />
        <input value={agent} onChange={(event) => setAgent(event.target.value)} placeholder={t("Agent")} />
        <label>
          {t("From")}
          <input type="datetime-local" value={since} onChange={(event) => setSince(event.target.value)} />
        </label>
        <label>
          {t("To")}
          <input type="datetime-local" value={until} onChange={(event) => setUntil(event.target.value)} />
        </label>
        <button type="submit" className="button primary small" disabled={busy}>{t("Search")}</button>
      </form>
      {error && <div className="live-error">{error}</div>}
      <ol className="live-feed">
        {items.map((card) => (
          <li key={card.task_key}>
            <StatusPill status={card.status} />
            <button type="button" className="live-link" onClick={() => onOpen(card.task_key)}>{card.title}</button>
            <span>{card.owner ?? "—"}</span>
            <span className="live-seq">{relativeTime(card.updated_at)}</span>
          </li>
        ))}
      </ol>
      {items.length === 0 && !busy && <p className="live-empty">{t("No archived tasks match.")}</p>}
      {next && (
        <button type="button" className="button ghost small" disabled={busy} onClick={() => void search(next)}>
          {t("Load more")}
        </button>
      )}
    </div>
  );
}
