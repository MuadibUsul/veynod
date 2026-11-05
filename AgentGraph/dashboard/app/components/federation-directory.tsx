"use client";

import { useCallback, useEffect, useState } from "react";

import { useI18n } from "../i18n";
import type { ApiRequest } from "../types";

/*
 * Directory (federation P4-2): workspaces that chose to be listed, ranked by
 * a published formula. Each result shows why it is where it is. Listing
 * this workspace is a person's choice; nothing is listed by default.
 */

type Factor = {
  factor: string;
  value: number;
  weight: number | string;
  contribution: number | null;
  why: string;
};

type Card = {
  fid: string;
  handle: string | null;
  headline: string;
  capabilities: string[];
  availability: string;
  rank: { score: number; position: number; explanation: Factor[] };
};

type Disclosure = Record<"completion" | "acceptance" | "response" | "skills" | "cooperation",
  boolean>;
type Listing = {
  listed: boolean;
  headline: string;
  capabilities: string[];
  availability: string;
  disclosure: Disclosure;
};

const DISCLOSURE_LABEL: Record<keyof Disclosure, string> = {
  completion: "Delivery rate",
  acceptance: "Acceptance rate",
  response: "Response speed",
  skills: "Skills",
  cooperation: "How many we worked with",
};

const FACTOR_LABEL: Record<string, string> = {
  relevance: "Fits the request",
  evidence: "Track record",
  availability: "Available now",
  safety: "Safety",
  promotion: "Promotion",
};

export function DirectoryCard({ request, notify, onConnect }: {
  request: ApiRequest;
  notify: (message: string) => void;
  onConnect: (fid: string) => void;
}) {
  const { t } = useI18n();
  const [query, setQuery] = useState("");
  const [cards, setCards] = useState<Card[]>([]);
  const [open, setOpen] = useState<string | null>(null);
  const [mine, setMine] = useState<Listing | null>(null);
  const [draft, setDraft] = useState({ headline: "", tags: "", availability: "open" });
  const [shown, setShown] = useState<Disclosure>({
    completion: true, acceptance: true, response: true, skills: true, cooperation: true,
  });

  const search = useCallback(async () => {
    const params = new URLSearchParams({ limit: "20" });
    const value = query.trim();
    if (value) params.set(/^[a-z0-9][a-z0-9-]{1,31}$/.test(value) ? "capability" : "q", value);
    try {
      const found = await request<{ items: Card[] }>(`/v1/federation/directory?${params}`);
      setCards(found.items ?? []);
    } catch (error) {
      notify((error as Error)?.message ?? t("Something went wrong."));
    }
  }, [query, request, notify, t]);

  useEffect(() => {
    void search();
    request<Listing>("/v1/federation/directory/listing").then((value) => {
      setMine(value);
      setDraft({ headline: value.headline, tags: value.capabilities.join(", "),
        availability: value.availability });
      if (value.disclosure) setShown(value.disclosure);
    }).catch(() => setMine(null));
    // Load once; searching is explicit.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function save(listed: boolean) {
    try {
      const value = await request<Listing>("/v1/federation/directory/listing", {
        method: "PUT",
        body: JSON.stringify({
          listed,
          headline: draft.headline.trim(),
          capabilities: draft.tags.split(",").map((s) => s.trim().toLowerCase()).filter(Boolean),
          availability: draft.availability,
          disclosure: shown,
        }),
      });
      setMine(value);
      notify(t(listed ? "Listed in the directory" : "Removed from the directory"));
      await search();
    } catch (error) {
      notify((error as Error)?.message ?? t("Something went wrong."));
    }
  }

  return (
    <section className="lin-list" aria-label={t("Directory")}>
      <div className="lin-group"><i className="lamp cyan" />{t("Directory")}</div>
      <form className="lin-row" onSubmit={(event) => { event.preventDefault(); void search(); }}>
        <input value={query} onChange={(event) => setQuery(event.target.value)}
          placeholder={t("A capability tag or a few words")} aria-label={t("Search the directory")} />
        <button type="submit" className="button ghost small">{t("Search")}</button>
      </form>
      {cards.length === 0 && (
        <div className="lin-row quiet">
          <span className="lin-title">{t("No listed workspace matches yet.")}</span>
        </div>
      )}
      {cards.map((card) => (
        <div key={card.fid} className="skills-installed">
          <div className="lin-row">
            <span className="lin-title">
              #{card.rank.position} {card.handle ?? card.fid.slice(0, 22)}
              {card.headline && <span className="flow-aside"> · {card.headline}</span>}
            </span>
            <span className="flow-aside">{card.capabilities.join(", ")}</span>
            <button type="button" className="button ghost small"
              onClick={() => setOpen(open === card.fid ? null : card.fid)}>
              {t("Why this place")}
            </button>
            <button type="button" className="button primary small" onClick={() => onConnect(card.fid)}>
              {t("Ask to connect")}
            </button>
          </div>
          {open === card.fid && card.rank.explanation.map((factor) => (
            <div key={factor.factor} className="lin-row quiet">
              <span className="lin-title">{t(FACTOR_LABEL[factor.factor] ?? factor.factor)}</span>
              <span className="flow-aside">
                {typeof factor.weight === "number" ? `${factor.value} × ${factor.weight}`
                  : `× ${factor.value}`} · {factor.why}
              </span>
            </div>
          ))}
        </div>
      ))}
      <div className="lin-group">{t("This workspace in the directory")}</div>
      <div className="lin-row">
        <input value={draft.headline} maxLength={200} aria-label={t("Headline")}
          placeholder={t("What you offer, in one line")}
          onChange={(event) => setDraft({ ...draft, headline: event.target.value })} />
        <input value={draft.tags} aria-label={t("Capability tags")}
          placeholder={t("tags, comma separated")}
          onChange={(event) => setDraft({ ...draft, tags: event.target.value })} />
        <select value={draft.availability} aria-label={t("Availability")}
          onChange={(event) => setDraft({ ...draft, availability: event.target.value })}>
          <option value="open">{t("Taking work")}</option>
          <option value="busy">{t("Busy")}</option>
          <option value="closed">{t("Not taking work")}</option>
        </select>
        <button type="button" className="button primary small" onClick={() => void save(true)}>
          {mine?.listed ? t("Update listing") : t("List this workspace")}
        </button>
        {mine?.listed && (
          <button type="button" className="button ghost small" onClick={() => void save(false)}>
            {t("Unlist")}
          </button>
        )}
      </div>
      <div className="lin-row">
        <span className="lin-title">{t("Show to others")}</span>
        {(Object.keys(DISCLOSURE_LABEL) as (keyof Disclosure)[]).map((key) => (
          <label key={key} className="skills-permission">
            <input type="checkbox" checked={shown[key]}
              onChange={(event) => setShown({ ...shown, [key]: event.target.checked })} />
            {" "}{t(DISCLOSURE_LABEL[key])}
          </label>
        ))}
      </div>
      <div className="lin-row quiet">
        <span className="lin-title">
          {t("Hidden figures do not count for ranking. Sanctions and resolved complaints are always shown.")}
        </span>
      </div>
      <div className="lin-row quiet">
        <span className="lin-title">
          {t("Listing shows your headline, tags and summary figures. It never shows your tasks, chats or who you worked with.")}
        </span>
      </div>
    </section>
  );
}
