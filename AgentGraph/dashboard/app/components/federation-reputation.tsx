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
  governance: {
    sanctions_in_force: number;
    sanctions_ever: number;
    complaints: { resolved: number; upheld: number; dismissed: number;
      upheld_by_category: Record<string, number> };
  };
  cooperation: {
    counterparts: number;
    repeat_counterparts: number;
    longest_partnership_days: number;
  };
  proofs: number;
  integrity: {
    flags: string[];
    cold_start: boolean;
    cold_start_ends: string | null;
    excluded: { same_controller: number; pending_verification: number };
    discounted: { fresh_requester: number };
  };
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
  const hidden = (value: unknown) => Boolean((value as { hidden_by_owner?: boolean })
    ?.hidden_by_owner);
  const pct = (rate: Rate) => hidden(rate) ? t("not shown by the owner")
    : rate.enough_evidence && rate.rate !== null
      ? `${Math.round(rate.rate * 100)}%` : t("not enough evidence");
  const off = t("not shown by the owner");
  const rows: [string, string, string][] = [
    hidden(page.completion) ? [t("Delivers work it takes on"), off, ""]
      : [t("Delivers work it takes on"), pct(page.completion),
        t("{n} signed records", { n: page.completion.samples })],
    hidden(page.acceptance) ? [t("Work accepted by the requester"), off, ""]
      : [t("Work accepted by the requester"), pct(page.acceptance),
        t("first time: {rate}", { rate: pct(page.acceptance.first_pass) })],
    hidden(page.response) ? [t("Answers delegation offers"), off, ""]
      : [t("Answers delegation offers"),
        page.response.enough_evidence && page.response.median_seconds !== null
          ? `${Math.max(1, Math.round(page.response.median_seconds / 60))} min`
          : t("not enough evidence"),
        t("{n} offers", { n: page.response.samples })],
    hidden(page.skills) ? [t("Skills"), off, ""]
      : [t("Skills"), page.skills.enough_evidence && page.skills.average !== null
        ? `${page.skills.average} / 5` : t("not enough ratings"),
        t("{n} published", { n: page.skills.published })],
    [t("Sanctions"), String(page.governance.sanctions_in_force),
      t("{n} ever", { n: page.governance.sanctions_ever })],
    [t("Resolved complaints"), t("{upheld} upheld", { upheld: page.governance.complaints.upheld }),
      t("{dismissed} dismissed · {resolved} resolved in all", {
        dismissed: page.governance.complaints.dismissed,
        resolved: page.governance.complaints.resolved })],
    hidden(page.cooperation) ? [t("Has worked with"), off, ""]
      : [t("Has worked with"), t("{n} partner workspaces", { n: page.cooperation.counterparts }),
        t("{n} came back · longest {d} days", { n: page.cooperation.repeat_counterparts,
          d: page.cooperation.longest_partnership_days })],
  ];
  return (
    <div className="skills-entry" aria-label={t("Reputation")}>
      <div className="lin-group">
        {t("Reputation")}
        <span className="flow-aside">{t("No single score: each line has its own evidence")}</span>
      </div>
      {(page.integrity.cold_start || page.integrity.excluded.same_controller > 0
        || page.integrity.excluded.pending_verification > 0
        || page.integrity.discounted.fresh_requester > 0) && (
        <div className="lin-row quiet">
          <span className="lin-title">
            {page.integrity.cold_start && page.integrity.cold_start_ends
              && `${t("New address: ranked as neutral until {date}", {
                date: page.integrity.cold_start_ends.slice(0, 10) })}. `}
            {page.integrity.excluded.same_controller > 0
              && `${t("{n} records from the same owner are not counted", {
                n: page.integrity.excluded.same_controller })}. `}
            {page.integrity.excluded.pending_verification > 0
              && `${t("{n} records await a review before they count", {
                n: page.integrity.excluded.pending_verification })}. `}
            {page.integrity.discounted.fresh_requester > 0
              && `${t("{n} records from new addresses count for less", {
                n: page.integrity.discounted.fresh_requester })}.`}
          </span>
        </div>
      )}
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
