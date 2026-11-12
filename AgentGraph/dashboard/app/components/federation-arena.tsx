"use client";

import { useCallback, useEffect, useState } from "react";

import { useI18n } from "../i18n";
import type { ApiRequest } from "../types";

/*
 * Resource rights and the Agent Poker Arena (federation P6, R1-R3).
 * NO ECONOMIC VALUE: DEMO_M and arena chips cannot be bought, withdrawn or
 * exchanged, and never pay a real model. Read-only here; play happens through
 * the API (people, or agents their owner allowed). The card stays hidden while
 * the deployment has the feature off (the routes answer 404).
 */

type Wallet = {
  capacity: { spec_id: string; unreserved_tokens: number; consumed_tokens: number }[];
  voucher_tokens_held: number;
  notice: string;
};
type Voucher = {
  voucher_id: string; issuer: string; beneficiary: string; origin: string;
  face_amount: number; available_amount: number; status: string;
};
type Obligation = {
  obligation_id: string; obligor: string; obligee: string; amount: number; state: string;
};
type Match = {
  match_id: string; state: string; stake: number; my_seat: number | null; model_class: string;
};

const M = 1_000_000;
const asM = (tokens: number) => `${Number((tokens / M).toFixed(6))} DEMO_M`;

export function ArenaCard({ request }: { request: ApiRequest }) {
  const { t } = useI18n();
  const [off, setOff] = useState(false);
  const [wallet, setWallet] = useState<Wallet | null>(null);
  const [held, setHeld] = useState<Voucher[]>([]);
  const [owed, setOwed] = useState<Obligation[]>([]);
  const [restricted, setRestricted] = useState(false);
  const [matches, setMatches] = useState<Match[]>([]);

  const load = useCallback(async () => {
    try {
      const w = await request<Wallet>("/v1/federation/rights/wallet");
      setWallet(w);
      const v = await request<{ held: Voucher[] }>("/v1/federation/rights/vouchers");
      setHeld(v.held ?? []);
    } catch {
      setOff(true); // resource rights are off on this deployment
      return;
    }
    try {
      const [o, m] = await Promise.all([
        request<{ owed_by_me: Obligation[]; restricted: boolean }>(
          "/v1/federation/arena/obligations"),
        request<{ items: Match[] }>("/v1/federation/arena/matches"),
      ]);
      setOwed(o.owed_by_me ?? []);
      setRestricted(Boolean(o.restricted));
      setMatches(m.items ?? []);
    } catch {
      // the arena itself is off; resource rights still show
    }
  }, [request]);

  useEffect(() => { void load(); }, [load]);

  if (off) return null;
  return (
    <section className="lin-list" aria-label={t("Arena (simulated)")}>
      <div className="lin-group">
        <i className="lamp amber" />{t("Arena (simulated)")}
        <span className="flow-aside">{t("No economic value; not redeemable or exchangeable")}</span>
      </div>
      {wallet && (
        <div className="lin-row">
          <span className="lin-title">{t("Simulated capacity")}</span>
          <strong>{asM(wallet.capacity.reduce((n, c) => n + c.unreserved_tokens, 0))}</strong>
          <span className="flow-aside">
            {t("{n} held in vouchers", { n: asM(wallet.voucher_tokens_held) })}
          </span>
        </div>
      )}
      {restricted && (
        <div className="lin-row">
          <i className="lamp red" />
          <span className="lin-title">
            {t("New arena matches are paused until an obligation is delivered or resolved")}
          </span>
        </div>
      )}
      {held.filter((v) => v.status === "ACTIVE").map((v) => (
        <div className="lin-row" key={v.voucher_id}>
          <span className="lin-title">{t("Voucher from {who}", { who: v.issuer })}</span>
          <span className="flow-aside">{asM(v.available_amount)} / {asM(v.face_amount)}</span>
        </div>
      ))}
      {owed.filter((o) => !["FULFILLED", "REMEDIATED", "RESOLVED"].includes(o.state)).map((o) => (
        <div className="lin-row" key={o.obligation_id}>
          <span className="lin-title">{t("Owed to {who}", { who: o.obligee })}</span>
          <span className="flow-aside">{asM(o.amount)} · {o.state}</span>
        </div>
      ))}
      {matches.slice(0, 5).map((m) => (
        <div className="lin-row" key={m.match_id}>
          <span className="lin-title">{m.match_id}</span>
          <span className="flow-aside">{m.model_class} · {asM(m.stake)} · {m.state}</span>
        </div>
      ))}
    </section>
  );
}
