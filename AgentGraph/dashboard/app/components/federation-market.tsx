"use client";

import { useCallback, useEffect, useState } from "react";

import { useI18n } from "../i18n";
import type { ApiRequest } from "../types";

/*
 * Market (federation P5-1): simulated credits only (SIM), never money.
 * Offers put a price on delegated work or a skill version; accepting one
 * holds the price in escrow; it is released when the work is accepted (or
 * the skill installed) and refunded on cancellation. People decide.
 */

type Wallet = { available: number; held_in_escrow: number; notice: string };
type Offer = {
  offer_id: string; kind: string; seller: string; buyer: string | null; subject: string;
  price: number; state: string;
};
type Contract = {
  contract_id: string; kind: string; seller: string; buyer: string; subject: string;
  price: number; state: string; in_escrow: number;
  receipt: { amount: number; paid_to: string } | null;
};

export function MarketCard({ request, notify, myFid }: {
  request: ApiRequest; notify: (message: string) => void; myFid: string;
}) {
  const { t } = useI18n();
  const [wallet, setWallet] = useState<Wallet | null>(null);
  const [offers, setOffers] = useState<Offer[]>([]);
  const [contracts, setContracts] = useState<Contract[]>([]);
  const [draft, setDraft] = useState({ kind: "task", subject: "", price: "", buyer: "" });
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const [w, o, c] = await Promise.all([
        request<Wallet>("/v1/federation/market/wallet"),
        request<{ items: Offer[] }>("/v1/federation/market/offers"),
        request<{ items: Contract[] }>("/v1/federation/market/contracts"),
      ]);
      setWallet(w);
      setOffers(o.items ?? []);
      setContracts(c.items ?? []);
    } catch (error) {
      notify((error as Error)?.message ?? t("Something went wrong."));
    }
  }, [request, notify, t]);

  useEffect(() => { void load(); }, [load]);

  async function act(path: string, body: unknown, done: string) {
    setBusy(true);
    try {
      await request(path, { method: "POST", body: body === undefined ? undefined
        : JSON.stringify(body) });
      notify(t(done));
      await load();
    } catch (error) {
      notify((error as Error)?.message ?? t("Something went wrong."));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="lin-list" aria-label={t("Market (simulated)")}>
      <div className="lin-group">
        <i className="lamp amber" />{t("Market (simulated)")}
        <span className="flow-aside">{t("SIM demonstration credits, not money")}</span>
      </div>
      {wallet && (
        <div className="lin-row">
          <span className="lin-title">{t("Credits")}</span>
          <strong>{wallet.available} SIM</strong>
          <span className="flow-aside">{t("{n} held in escrow", { n: wallet.held_in_escrow })}</span>
        </div>
      )}
      {offers.filter((o) => o.state === "OPEN").map((offer) => (
        <div key={offer.offer_id} className="lin-row">
          <span className="lin-title">{t(offer.kind)} · {offer.subject}</span>
          <strong>{offer.price} SIM</strong>
          {offer.seller === myFid ? (
            <button type="button" className="button ghost small" disabled={busy}
              onClick={() => void act(`/v1/federation/market/offers/${offer.offer_id}/withdraw`,
                undefined, "Offer withdrawn")}>{t("Withdraw")}</button>
          ) : (
            <button type="button" className="button primary small" disabled={busy}
              onClick={() => void act(`/v1/federation/market/offers/${offer.offer_id}/accept`,
                undefined, "Accepted: the price is held in escrow")}>
              {t("Accept and hold in escrow")}
            </button>
          )}
        </div>
      ))}
      {contracts.map((contract) => (
        <div key={contract.contract_id} className="lin-row">
          <span className="lin-title">
            {contract.buyer === myFid ? t("Buying") : t("Selling")} · {t(contract.kind)} ·{" "}
            {contract.subject}
          </span>
          <span className="flow-aside">
            {contract.price} SIM · {t(contract.state)}
            {contract.in_escrow > 0 && ` · ${t("{n} held in escrow", { n: contract.in_escrow })}`}
            {contract.receipt && ` · ${t("paid {n}", { n: contract.receipt.amount })}`}
          </span>
          {contract.buyer === myFid && contract.state === "FUNDED" && (
            <button type="button" className="button ghost small" disabled={busy}
              onClick={() => void act(`/v1/federation/market/contracts/${contract.contract_id}/cancel`,
                undefined, "Cancelled and refunded")}>{t("Cancel")}</button>
          )}
        </div>
      ))}
      <form className="lin-row" onSubmit={(event) => {
        event.preventDefault();
        void act("/v1/federation/market/offers", {
          kind: draft.kind,
          subject: draft.subject.trim(),
          price: Number(draft.price),
          buyer: draft.buyer.trim() || undefined,
        }, "Offer sent");
      }}>
        <select value={draft.kind} aria-label={t("Kind")}
          onChange={(event) => setDraft({ ...draft, kind: event.target.value })}>
          <option value="task">{t("task")}</option>
          <option value="skill">{t("skill")}</option>
        </select>
        <input value={draft.subject} aria-label={t("Job id or skill@version")}
          placeholder={t("Job id or skill@version")}
          onChange={(event) => setDraft({ ...draft, subject: event.target.value })} />
        <input value={draft.price} inputMode="numeric" aria-label={t("Price (SIM)")}
          placeholder={t("Price (SIM)")}
          onChange={(event) => setDraft({ ...draft, price: event.target.value })} />
        <input value={draft.buyer} aria-label={t("Buyer (for a task)")}
          placeholder={t("Buyer (for a task)")}
          onChange={(event) => setDraft({ ...draft, buyer: event.target.value })} />
        <button type="submit" className="button primary small"
          disabled={busy || !draft.subject.trim() || !Number(draft.price)}>{t("Make an offer")}</button>
      </form>
    </section>
  );
}
