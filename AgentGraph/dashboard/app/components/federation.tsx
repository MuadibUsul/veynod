"use client";

import { IconFederation } from "./ui";

/**
 * Federation: several boards answering as one.
 *
 * The page is a placeholder until the join protocol ships, so it states what
 * the page will hold rather than showing an empty shell with tabs that do
 * nothing.
 */
export function FederationView({ t }: { t: (key: string) => string }) {
  return (
    <div className="empty-state">
      <div className="empty-mark">
        <IconFederation size={30} />
      </div>
      <h2>{t("Federation is coming soon")}</h2>
      <p>
        {t(
          "One board per team, joined so a task can cross into another team's board with its evidence. Nothing to set up yet.",
        )}
      </p>
      <ul className="federation-preview">
        <li>
          <strong>{t("Peers")}</strong>
          <span>{t("Which boards this one trusts, and the scope each may reach.")}</span>
        </li>
        <li>
          <strong>{t("Handoffs")}</strong>
          <span>{t("Tasks that leave this board, with the acceptance they keep there.")}</span>
        </li>
        <li>
          <strong>{t("Shared evidence")}</strong>
          <span>{t("Gate and merge results that travel with the commit, never a status alone.")}</span>
        </li>
      </ul>
    </div>
  );
}
