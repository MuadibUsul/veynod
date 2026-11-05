"use client";

import { useCallback, useEffect, useState } from "react";

import { useI18n } from "../i18n";
import type { ApiRequest } from "../types";

/*
 * Skills (federation P3): find a skill, read its entry, install it, rate it.
 *
 * Every number says where it comes from: the publisher's own words
 * (description, category, tags), other workspaces (downloads, ratings) or the
 * platform's measurement (how fast the publisher answers delegation offers).
 * An average or a response time with too few samples reads "not enough
 * evidence" rather than a weak number. Installing and rating are a person's
 * decisions; an executable skill installs only once its permissions are
 * accepted one by one.
 */

type Evidence = { enough_evidence: boolean };
type Rating = Evidence & { average: number | null; count: number };
type Response = Evidence & { median_seconds: number | null; samples: number; window_days: number };

type Hit = {
  name: string;
  description: string | null;
  category: string;
  capabilities: string[];
  kind: string | null;
  installable_version: string | null;
  digest: string | null;
  publisher: { fid: string; handle: string | null };
  downloads: number;
  rating: Rating;
  response: Response;
  price: { model: string };
};

type Entry = {
  name: string;
  address: { package: string; publisher: string; publisher_handle: string | null;
    pin: string | null; digest: string | null };
  description: string | null;
  category: string;
  capabilities: string[];
  kind: string | null;
  installable: boolean;
  downloads: { total: number };
  rating: Rating & { distribution: Record<string, number>;
    recent_reviews: { version: string; stars: number; review: string; at: string }[] };
  history: { version: string; digest: string; kind: string; permissions: string[]; state: string;
    published_at: string; yank_reason: string | null; downloads: number; rating: Rating }[];
  dependencies: { name: string; version: string; digest: string; state: string | null }[];
  dependants: { name: string; version: string }[];
  publisher: { fid: string; handle: string | null; packages: number; accepted_results: number;
    response: Response };
  representative_works: { name: string; description: string | null; downloads: number;
    rating: Rating }[];
};

type Installed = {
  name: string;
  version: string;
  digest: string;
  yanked: boolean;
  accepted_permissions: string[];
  declared_permissions: string[];
  update_policy: "locked" | "follow_patch" | "follow_minor";
  as_dependency_of: string | null;
};

type Update = {
  name: string;
  pinned_version: string;
  applies_on_its_own: boolean;
  needs_a_person_because: string[];
  available: {
    version: string;
    declared_permissions: string[];
    permissions: { added: string[]; removed: string[] };
    files: { added: string[]; removed: string[]; changed: string[] };
    dependencies_changed: boolean;
    sandbox: string;
  };
};

type HistoryLine = {
  action: string;
  from_version: string | null;
  version: string | null;
  accepted_permissions: string[];
  actor: string;
  at: string;
};

const POLICY_LABEL: Record<string, string> = {
  locked: "Stay on this version",
  follow_patch: "Follow patch releases",
  follow_minor: "Follow minor releases",
};

const CATEGORY_LABEL: Record<string, string> = {
  research: "Research", writing: "Writing", coding: "Coding", data: "Data", design: "Design",
  operations: "Operations", other: "Other",
};
const SORT_LABEL: Record<string, string> = {
  relevance: "Best match", downloads: "Most downloaded", rating: "Best rated",
  response: "Fastest to respond", recent: "Recently updated",
};
const RESPONSE_LIMITS = [0, 600, 3600, 86400];

function stars(rating: Rating, t: (s: string, v?: Record<string, string | number>) => string) {
  if (!rating.enough_evidence) {
    return rating.count
      ? t("{count} ratings, not enough to average", { count: rating.count })
      : t("No ratings yet");
  }
  return t("{average} / 5 from {count} ratings", { average: rating.average ?? 0,
    count: rating.count });
}

function speed(response: Response, t: (s: string, v?: Record<string, string | number>) => string) {
  if (!response.enough_evidence || response.median_seconds === null) {
    return t("Response time: not enough evidence");
  }
  const s = response.median_seconds;
  const text = s < 90 ? `${Math.round(s)} s` : s < 5400 ? `${Math.round(s / 60)} min`
    : `${Math.round(s / 3600)} h`;
  return t("Answers offers in {time} (median)", { time: text });
}

export function SkillsCard({ request, notify }: {
  request: ApiRequest;
  notify: (message: string) => void;
}) {
  const { t } = useI18n();
  const [query, setQuery] = useState("");
  const [category, setCategory] = useState("");
  const [minRating, setMinRating] = useState("");
  const [maxResponse, setMaxResponse] = useState(0);
  const [sort, setSort] = useState("relevance");
  const [hits, setHits] = useState<Hit[]>([]);
  const [total, setTotal] = useState(0);
  const [open, setOpen] = useState<Entry | null>(null);
  const [installed, setInstalled] = useState<Installed[]>([]);
  const [updates, setUpdates] = useState<Update[]>([]);
  const [busy, setBusy] = useState(false);

  const search = useCallback(async () => {
    const params = new URLSearchParams({ sort, limit: "20" });
    if (query.trim()) params.set("q", query.trim());
    if (category) params.set("category", category);
    if (minRating) params.set("min_rating", minRating);
    if (maxResponse) params.set("max_response_seconds", String(maxResponse));
    try {
      const [found, mine, pending] = await Promise.all([
        request<{ items: Hit[]; total: number }>(`/v1/federation/registry/search?${params}`),
        request<{ items: Installed[] }>("/v1/federation/registry/installs"),
        request<{ items: Update[] }>("/v1/federation/registry/updates"),
      ]);
      setHits(found.items ?? []);
      setTotal(found.total ?? 0);
      setInstalled(mine.items ?? []);
      setUpdates(pending.items ?? []);
    } catch (error) {
      notify((error as Error)?.message ?? t("Something went wrong."));
    }
  }, [request, notify, t, query, category, minRating, maxResponse, sort]);

  useEffect(() => {
    void search();
    // Filters other than the words apply as soon as they change.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [category, minRating, maxResponse, sort]);

  async function show(name: string) {
    try {
      setOpen(await request<Entry>(`/v1/federation/registry/entries/${name}`));
    } catch (error) {
      notify((error as Error)?.message ?? t("Something went wrong."));
    }
  }

  async function act(path: string, body: unknown, done: string, method = "POST") {
    setBusy(true);
    try {
      await request(path, { method, body: JSON.stringify(body) });
      notify(t(done));
      await search();
      if (open) await show(open.name);
    } catch (error) {
      notify((error as Error)?.message ?? t("Something went wrong."));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="lin-list" aria-label={t("Skills")}>
      {installed.length > 0 && (
        <InstalledList installed={installed} updates={updates} busy={busy} act={act}
          request={request} notify={notify} />
      )}
      <div className="lin-group"><i className="lamp cyan" />{t("Find a skill")}</div>
      <form className="lin-row" onSubmit={(event) => { event.preventDefault(); void search(); }}>
        <input value={query} onChange={(event) => setQuery(event.target.value)}
          placeholder={t("What should it do?")} aria-label={t("What should it do?")} />
        <button type="submit" className="button ghost small">{t("Search")}</button>
      </form>
      <div className="lin-row skills-filters">
        <select value={category} onChange={(event) => setCategory(event.target.value)}
          aria-label={t("Category")}>
          <option value="">{t("Any category")}</option>
          {Object.entries(CATEGORY_LABEL).map(([key, label]) => (
            <option key={key} value={key}>{t(label)}</option>
          ))}
        </select>
        <select value={minRating} onChange={(event) => setMinRating(event.target.value)}
          aria-label={t("Rating")}>
          <option value="">{t("Any rating")}</option>
          <option value="4">{t("4 stars and up")}</option>
          <option value="3">{t("3 stars and up")}</option>
        </select>
        <select value={maxResponse} onChange={(event) => setMaxResponse(Number(event.target.value))}
          aria-label={t("Response speed")}>
          {RESPONSE_LIMITS.map((limit) => (
            <option key={limit} value={limit}>
              {limit === 0 ? t("Any response speed") : limit === 600 ? t("Answers within 10 min")
                : limit === 3600 ? t("Answers within 1 h") : t("Answers within a day")}
            </option>
          ))}
        </select>
        <select value={sort} onChange={(event) => setSort(event.target.value)}
          aria-label={t("Sort")}>
          {Object.entries(SORT_LABEL).map(([key, label]) => (
            <option key={key} value={key}>{t(label)}</option>
          ))}
        </select>
        <span className="flow-aside">{t("All skills are free for now")}</span>
      </div>
      {hits.length === 0 && (
        <div className="lin-row quiet">
          <span className="lin-title">{t("No skill matches. Loosen a filter or try other words.")}</span>
        </div>
      )}
      {hits.map((hit) => {
        const pin = installed.find((item) => item.name === hit.name);
        return (
          <div key={hit.name} className="lin-row">
            <span className="lin-title">
              <button type="button" className="link-button" onClick={() => void show(hit.name)}>
                {hit.name}
              </button>
              {hit.description && <span className="flow-aside"> · {hit.description}</span>}
            </span>
            <span className="flow-aside">
              {t(CATEGORY_LABEL[hit.category] ?? hit.category)} · {stars(hit.rating, t)} ·{" "}
              {t("{count} workspaces", { count: hit.downloads })} · {speed(hit.response, t)}
            </span>
            {pin && (
              <span className="flow-aside">
                {pin.yanked ? t("Installed {version}, since withdrawn", { version: pin.version })
                  : t("Installed {version}", { version: pin.version })}
              </span>
            )}
          </div>
        );
      })}
      {total > hits.length && (
        <div className="lin-row quiet">
          <span className="lin-title">{t("Showing {shown} of {total}", { shown: hits.length,
            total })}</span>
        </div>
      )}
      {open && (
        <SkillEntry entry={open} busy={busy} installed={installed.find((i) => i.name === open.name)}
          onClose={() => setOpen(null)} onOpen={(name) => void show(name)} act={act} />
      )}
    </section>
  );
}

function SkillEntry({ entry, busy, installed, onClose, onOpen, act }: {
  entry: Entry;
  busy: boolean;
  installed?: Installed;
  onClose: () => void;
  onOpen: (name: string) => void;
  act: (path: string, body: unknown, done: string, method?: string) => Promise<void>;
}) {
  const { t } = useI18n();
  const current = entry.history.find((item) => item.state === "published");
  const permissions = current?.permissions ?? [];
  const [accepted, setAccepted] = useState<string[]>([]);
  const [myStars, setMyStars] = useState(5);
  const [review, setReview] = useState("");
  const allAccepted = permissions.every((p) => accepted.includes(p));

  return (
    <div className="skills-entry" aria-label={t("Skill entry")}>
      <div className="lin-group">
        <i className="lamp cyan" />{entry.name}
        <button type="button" className="button ghost small" onClick={onClose}>{t("Close")}</button>
      </div>
      <div className="lin-row">
        <span className="lin-title">{t("Address")}</span>
        <code className="federation-id">{entry.address.pin ?? entry.name}</code>
        <span className="flow-aside">
          {t("by")} {entry.address.publisher_handle ?? entry.address.publisher}
        </span>
      </div>
      {entry.address.digest && (
        <div className="lin-row">
          <span className="lin-title">{t("Content fingerprint")}</span>
          <code className="federation-id">{entry.address.digest}</code>
        </div>
      )}
      <div className="lin-row">
        <span className="lin-title">{entry.description}</span>
        <span className="flow-aside">
          {t("Publisher's own description")} · {entry.capabilities.join(", ")}
        </span>
      </div>
      <div className="lin-row">
        <span className="lin-title">{stars(entry.rating, t)}</span>
        <span className="flow-aside">
          {[5, 4, 3, 2, 1].map((s) => `${s}★ ${entry.rating.distribution[String(s)] ?? 0}`)
            .join(" · ")}
        </span>
      </div>
      {entry.rating.recent_reviews.map((item) => (
        <div key={`${item.at}-${item.version}`} className="lin-row quiet">
          <span className="lin-title">{"★".repeat(item.stars)} {item.review}</span>
          <span className="flow-aside">{item.version}</span>
        </div>
      ))}
      <div className="lin-row">
        <span className="lin-title">{t("Publisher's track record")}</span>
        <span className="flow-aside">
          {t("{count} skills", { count: entry.publisher.packages })} ·{" "}
          {t("{count} delegated jobs accepted", { count: entry.publisher.accepted_results })} ·{" "}
          {speed(entry.publisher.response, t)}
        </span>
      </div>

      <div className="lin-group">{t("History")}</div>
      {entry.history.map((item) => (
        <div key={item.version} className="lin-row">
          <span className="lin-title">{item.version}</span>
          <span className="flow-aside">
            {item.state === "yanked"
              ? t("Withdrawn: {reason}", { reason: item.yank_reason ?? "" })
              : t("Published {date}", { date: item.published_at.slice(0, 10) })}
            {" · "}{t("{count} workspaces", { count: item.downloads })}
            {item.permissions.length > 0 && ` · ${t("needs")} ${item.permissions.join(", ")}`}
          </span>
          <code className="federation-id">{item.digest.slice(0, 19)}…</code>
        </div>
      ))}

      {(entry.dependencies.length > 0 || entry.dependants.length > 0) && (
        <div className="lin-group">{t("Dependencies")}</div>
      )}
      {entry.dependencies.map((dep) => (
        <div key={dep.digest} className="lin-row">
          <span className="lin-title">{t("Uses")} {dep.name}@{dep.version}</span>
          {dep.state === "yanked" && <span className="flow-aside">{t("withdrawn")}</span>}
        </div>
      ))}
      {entry.dependants.map((dep) => (
        <div key={dep.name} className="lin-row">
          <span className="lin-title">{t("Used by")}{" "}
            <button type="button" className="link-button" onClick={() => onOpen(dep.name)}>
              {dep.name}
            </button>
          </span>
        </div>
      ))}

      {entry.representative_works.length > 0 && (
        <div className="lin-group">{t("More from this publisher")}</div>
      )}
      {entry.representative_works.map((work) => (
        <div key={work.name} className="lin-row">
          <span className="lin-title">
            <button type="button" className="link-button" onClick={() => onOpen(work.name)}>
              {work.name}
            </button>
          </span>
          <span className="flow-aside">
            {t("{count} workspaces", { count: work.downloads })} · {stars(work.rating, t)}
          </span>
        </div>
      ))}

      <div className="lin-group">{t("Use it")}</div>
      {current && installed?.version !== current.version && (
        <div className="lin-row">
          {permissions.length > 0 && (
            <span className="lin-title">
              {t("This skill runs code and asks for:")}{" "}
              {permissions.map((p) => (
                <label key={p} className="skills-permission">
                  <input type="checkbox" checked={accepted.includes(p)}
                    onChange={(event) => setAccepted(event.target.checked
                      ? [...accepted, p] : accepted.filter((x) => x !== p))} /> {p}
                </label>
              ))}
            </span>
          )}
          <button type="button" className="button small" disabled={busy || !allAccepted}
            onClick={() => void act("/v1/federation/registry/installs",
              { name: entry.name, version: current.version, accept_permissions: accepted },
              "Installed and pinned")}>
            {t("Install {version}", { version: current.version })}
          </button>
        </div>
      )}
      {!current && (
        <div className="lin-row quiet">
          <span className="lin-title">{t("Every version was withdrawn; nothing can be installed.")}</span>
        </div>
      )}
      {installed && (
        <form className="lin-row" onSubmit={(event) => {
          event.preventDefault();
          void act(`/v1/federation/registry/packages/${entry.name}/rating`,
            { stars: myStars, review: review.trim() || null }, "Rating saved", "PUT");
        }}>
          <select value={myStars} onChange={(event) => setMyStars(Number(event.target.value))}
            aria-label={t("Your rating")}>
            {[5, 4, 3, 2, 1].map((s) => <option key={s} value={s}>{"★".repeat(s)}</option>)}
          </select>
          <input value={review} maxLength={1000} onChange={(event) => setReview(event.target.value)}
            placeholder={t("A short review (optional)")} aria-label={t("Review")} />
          <button type="submit" className="button ghost small" disabled={busy}>{t("Rate")}</button>
        </form>
      )}
    </div>
  );
}

function InstalledList({ installed, updates, busy, act, request, notify }: {
  installed: Installed[];
  updates: Update[];
  busy: boolean;
  act: (path: string, body: unknown, done: string, method?: string) => Promise<void>;
  request: ApiRequest;
  notify: (message: string) => void;
}) {
  const { t } = useI18n();
  const [open, setOpen] = useState<string | null>(null);
  const [lines, setLines] = useState<HistoryLine[]>([]);
  const [accepted, setAccepted] = useState<Record<string, string[]>>({});

  async function showHistory(name: string) {
    if (open === name) {
      setOpen(null);
      return;
    }
    try {
      const page = await request<{ items: HistoryLine[] }>(
        `/v1/federation/registry/installs/${name}/history`);
      setLines(page.items ?? []);
      setOpen(name);
    } catch (error) {
      notify((error as Error)?.message ?? t("Something went wrong."));
    }
  }

  const toggle = (name: string, permission: string, on: boolean) => setAccepted((prev) => ({
    ...prev,
    [name]: on ? [...(prev[name] ?? []), permission]
      : (prev[name] ?? []).filter((p) => p !== permission),
  }));

  return (
    <>
      <div className="lin-group"><i className="lamp green" />{t("Installed here")}</div>
      {installed.map((pin) => {
        const update = updates.find((u) => u.name === pin.name);
        const wanted = update?.available.declared_permissions ?? [];
        const ticked = accepted[pin.name] ?? [];
        const ready = wanted.every((p) => ticked.includes(p));
        return (
          <div key={pin.name} className="skills-installed">
            <div className="lin-row">
              <span className="lin-title">{pin.name} <code>{pin.version}</code></span>
              <span className="flow-aside">
                {pin.accepted_permissions.length
                  ? t("Allowed: {list}", { list: pin.accepted_permissions.join(", ") })
                  : t("Reads text only, no permissions")}
                {pin.yanked && ` · ${t("this version was withdrawn")}`}
                {pin.as_dependency_of && ` · ${t("needed by {name}", { name: pin.as_dependency_of })}`}
              </span>
              <select value={pin.update_policy} aria-label={t("Updates")} disabled={busy}
                onChange={(event) => void act(`/v1/federation/registry/installs/${pin.name}/policy`,
                  { update_policy: event.target.value }, "Update choice saved", "PUT")}>
                {Object.entries(POLICY_LABEL).map(([key, label]) => (
                  <option key={key} value={key}>{t(label)}</option>
                ))}
              </select>
              <button type="button" className="button ghost small"
                onClick={() => void showHistory(pin.name)}>{t("History")}</button>
            </div>
            {update && (
              <div className="lin-row">
                <span className="lin-title">
                  {t("Version {version} is available", { version: update.available.version })}
                  {update.available.permissions.added.length > 0 && (
                    <strong> · {t("asks for new permissions: {list}",
                      { list: update.available.permissions.added.join(", ") })}</strong>
                  )}
                  {update.available.files.added.length > 0
                    && ` · ${t("adds files: {list}", { list: update.available.files.added.join(", ") })}`}
                  {update.available.sandbox !== "passed" && ` · ${t("fails the sandbox check")}`}
                </span>
                {wanted.map((p) => (
                  <label key={p} className="skills-permission">
                    <input type="checkbox" checked={ticked.includes(p)}
                      onChange={(event) => toggle(pin.name, p, event.target.checked)} /> {p}
                  </label>
                ))}
                <button type="button" className="button small"
                  disabled={busy || !ready || update.available.sandbox !== "passed"}
                  onClick={() => void act(`/v1/federation/registry/installs/${pin.name}/update`,
                    { version: update.available.version, accept_permissions: ticked },
                    "Updated")}>
                  {t("Update")}
                </button>
              </div>
            )}
            {open === pin.name && lines.map((line, index) => (
              <div key={`${line.at}-${index}`} className="lin-row quiet">
                <span className="lin-title">
                  {t(line.action)} {line.from_version ? `${line.from_version} → ` : ""}
                  {line.version ?? ""}
                </span>
                <span className="flow-aside">
                  {line.accepted_permissions.join(", ") || "—"} · {line.at.slice(0, 16)}
                </span>
                {line.version && line.version !== pin.version && line.action !== "uninstall" && (
                  <button type="button" className="button ghost small" disabled={busy}
                    onClick={() => void act(`/v1/federation/registry/installs/${pin.name}/rollback`,
                      { version: line.version, accept_permissions: line.accepted_permissions },
                      "Restored")}>
                    {t("Restore {version}", { version: line.version })}
                  </button>
                )}
              </div>
            ))}
          </div>
        );
      })}
    </>
  );
}
