"use client";

import { useCallback, useEffect, useState } from "react";

import { useI18n } from "../i18n";
import type { ApiRequest } from "../types";
import { Spinner } from "./ui";

type Verdict = "works" | "broken" | "change";

type Delivered = {
  task_key: string;
  title: string;
  status: string;
  delivered_at: string;
  owner_report: { what_changed: string; how_to_try: string; not_yet_done: string } | null;
  acceptance: {
    verdict: Verdict;
    note: string | null;
    follow_up_task_key: string | null;
    decided_at: string;
  } | null;
};

const VERDICT_LABEL: Record<Verdict, string> = {
  works: "It works",
  broken: "It doesn't work",
  change: "Change something",
};

function AcceptanceItem({
  item,
  request,
  onSaved,
}: {
  item: Delivered;
  request: ApiRequest;
  onSaved: (message: string) => void;
}) {
  const { t } = useI18n();
  const [asking, setAsking] = useState<Verdict | null>(null);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function send(verdict: Verdict) {
    setBusy(true);
    setError(null);
    try {
      await request(`/v1/human/acceptance/${encodeURIComponent(item.task_key)}`, {
        method: "POST",
        body: JSON.stringify({ verdict, note: verdict === "works" ? null : note }),
      });
      setAsking(null);
      setNote("");
      onSaved(verdict === "works" ? t("Thanks, noted.") : t("Saved. An agent will take it from here."));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy(false);
    }
  }

  const report = item.owner_report;
  return (
    <article className="acceptance-item">
      <header>
        <strong>{item.title}</strong>
        <small>{new Date(item.delivered_at).toLocaleString()}</small>
      </header>
      {report ? (
        <dl className="acceptance-report">
          <dt>{t("What changed")}</dt>
          <dd>{report.what_changed}</dd>
          <dt>{t("How to try it")}</dt>
          <dd>{report.how_to_try}</dd>
          {report.not_yet_done && (
            <>
              <dt>{t("Not done yet")}</dt>
              <dd>{report.not_yet_done}</dd>
            </>
          )}
        </dl>
      ) : (
        <p className="field-hint">{t("No plain-words report for this one yet.")}</p>
      )}
      {item.acceptance && (
        <p className={`acceptance-verdict ${item.acceptance.verdict}`}>
          {t(VERDICT_LABEL[item.acceptance.verdict])}
          {item.acceptance.note ? `: ${item.acceptance.note}` : ""}
          {item.acceptance.follow_up_task_key && (
            <small> · {t("Follow-up")} {item.acceptance.follow_up_task_key}</small>
          )}
        </p>
      )}
      {error && <div className="inline-error">{error}</div>}
      {asking ? (
        <div className="acceptance-note">
          <textarea
            value={note}
            onChange={(event) => setNote(event.target.value)}
            placeholder={
              asking === "broken"
                ? t("What did you try, and what happened?")
                : t("What should be different?")
            }
            rows={3}
            maxLength={4000}
          />
          <div className="acceptance-actions">
            <button
              type="button"
              className="button primary"
              disabled={busy || !note.trim()}
              onClick={() => send(asking)}
            >
              {busy ? <Spinner /> : null} {t("Send")}
            </button>
            <button type="button" className="button" onClick={() => setAsking(null)}>
              {t("Cancel")}
            </button>
          </div>
        </div>
      ) : (
        <div className="acceptance-actions">
          <button type="button" className="button primary" disabled={busy} onClick={() => send("works")}>
            {t(VERDICT_LABEL.works)}
          </button>
          <button type="button" className="button" onClick={() => setAsking("broken")}>
            {t(VERDICT_LABEL.broken)}
          </button>
          <button type="button" className="button" onClick={() => setAsking("change")}>
            {t(VERDICT_LABEL.change)}
          </button>
        </div>
      )}
    </article>
  );
}

export function AcceptanceCard({
  request,
  notify,
}: {
  request: ApiRequest;
  notify: (message: string) => void;
}) {
  const { t } = useI18n();
  const [items, setItems] = useState<Delivered[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    request<{ items: Delivered[] }>("/v1/human/acceptance")
      .then((value) => setItems(value.items))
      .catch((caught: unknown) => setError(caught instanceof Error ? caught.message : String(caught)));
  }, [request]);

  useEffect(load, [load]);

  return (
    <section className="card">
      <div className="card-head">
        <div>
          <h2>{t("Try and accept")}</h2>
          <p>{t("Try what was delivered, then say in your own words whether it works. No technical details needed.")}</p>
        </div>
      </div>
      {items === null && !error && <Spinner />}
      {error && <div className="inline-error">{error}</div>}
      {items !== null && items.length === 0 && (
        <p className="field-hint">{t("Nothing new to try.")}</p>
      )}
      {items !== null && items.length > 0 && (
        <div className="acceptance-list">
          {items.map((item) => (
            <AcceptanceItem
              key={item.task_key}
              item={item}
              request={request}
              onSaved={(message) => {
                notify(message);
                load();
              }}
            />
          ))}
        </div>
      )}
    </section>
  );
}
