"use client";

import { useEffect, useState } from "react";

import { useI18n } from "../i18n";
import type { ApiRequest } from "../types";

/*
 * Reputation (federation P4-1): a workspace's record as a provider, one
 * measure per dimension, each with its sample size. There is no single
 * score. Rates come from proofs signed by the other side of each job.
 */

type Rate = {
  rate: number | null;
  samples: number;
  enough_evidence: boolean;
};

type Profile = {
  completion: Rate & { counts: Record<string, number> };
  acceptance: Rate & { first_pass: Rate };
  response: { median_seconds: number | null; samples: number; enough_evidence: boolean };
  skills: { published: number; average: number | null; count: number; enough_evidence: boolean };
  governance: { sanctions_in_force: number; sanctions_ever: number };
  cooperation: {
    counterparts: number;
    repeat_counterparts: number;
    longest_partnership_days: number;
  };
  proofs: number;
};

export function ReputationPanel({ fid, request }: { fid: string; request: ApiRequest }) {
  const { t } = useI18n();
  const [page, setPage] = useState<Profile | null>(null);

  useEffect(() => {
    let live = true;
    request<Profile>(`/v1/federation/reputation/${fid}`)
      .then((value) => { if (live) setPage(value); })
      .catch(() => { if (live) setPage(null); });
    return () => { live = false; };
  }, [fid, request]);

  if (!page) return null;
  const pct = (rate: Rate) => rate.enough_evidence && rate.rate !== null
    ? `${Math.round(rate.rate * 100)}%` : t("not enough evidence");
  const rows: [string, string, string][] = [
    [t("Delivers work it takes on"), pct(page.completion),
      t("{n} signed records", { n: page.completion.samples })],
    [t("Work accepted by the requester"), pct(page.acceptance),
      t("first time: {rate}", { rate: pct(page.acceptance.first_pass) })],
    [t("Answers delegation offers"),
      page.response.enough_evidence && page.response.median_seconds !== null
        ? `${Math.max(1, Math.round(page.response.median_seconds / 60))} min`
        : t("not enough evidence"),
      t("{n} offers", { n: page.response.samples })],
    [t("Skills"), page.skills.enough_evidence && page.skills.average !== null
      ? `${page.skills.average} / 5` : t("not enough ratings"),
      t("{n} published", { n: page.skills.published })],
    [t("Sanctions"), String(page.governance.sanctions_in_force),
      t("{n} ever", { n: page.governance.sanctions_ever })],
    [t("Has worked with"), t("{n} partner workspaces", { n: page.cooperation.counterparts }),
      t("{n} came back · longest {d} days", { n: page.cooperation.repeat_counterparts,
        d: page.cooperation.longest_partnership_days })],
  ];
  return (
    <div className="skills-entry" aria-label={t("Reputation")}>
      <div className="lin-group">
        {t("Reputation")}
        <span className="flow-aside">{t("No single score: each line has its own evidence")}</span>
      </div>
      {rows.map(([label, value, note]) => (
        <div key={label} className="lin-row">
          <span className="lin-title">{label}</span>
          <strong>{value}</strong>
          <span className="flow-aside">{note}</span>
        </div>
      ))}
    </div>
  );
}
