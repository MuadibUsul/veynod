"use client";

import { useEffect, useState } from "react";

import { useI18n } from "../i18n";
import type { ApiRequest } from "../types";
import { Spinner } from "./ui";

type Mode = "manual" | "semi" | "full";

type Automation = {
  mode: Mode;
  auto_deploy_allowed: boolean;
  updated_by: string | null;
  updated_at: string | null;
  version: number;
};

const MODES: Array<{ mode: Mode; title: string; body: string }> = [
  {
    mode: "manual",
    title: "Ask me every step",
    body: "Agents stop and ask before each step. Nothing changes without your yes.",
  },
  {
    mode: "semi",
    title: "Small calls automatically",
    body: "Agents decide the small things themselves and ask once when money, deletion or an outside promise is involved.",
  },
  {
    mode: "full",
    title: "Do it all, I only accept the result",
    body: "Agents claim, build, test, review, merge and deploy. You only try the result and say whether it works.",
  },
];

export function AutomationModeCard({
  request,
  notify,
}: {
  request: ApiRequest;
  notify: (message: string) => void;
}) {
  const { t } = useI18n();
  const [mode, setMode] = useState<Mode>("manual");
  const [autoDeploy, setAutoDeploy] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    request<Automation>("/v1/human/automation")
      .then((value) => {
        if (cancelled) return;
        setMode(value.mode);
        setAutoDeploy(value.auto_deploy_allowed);
        setLoaded(true);
      })
      .catch((caught: unknown) => {
        if (!cancelled) setError(caught instanceof Error ? caught.message : String(caught));
      });
    return () => {
      cancelled = true;
    };
  }, [request]);

  async function save() {
    setBusy(true);
    setError(null);
    try {
      const value = await request<Automation>("/v1/human/automation", {
        method: "PUT",
        body: JSON.stringify({ mode, auto_deploy_allowed: autoDeploy }),
      });
      setMode(value.mode);
      setAutoDeploy(value.auto_deploy_allowed);
      notify(t("Automation mode updated."));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="card">
      <div className="card-head">
        <div>
          <h2>{t("Automation mode")}</h2>
          <p>{t("How much should agents do without asking you?")}</p>
        </div>
      </div>
      {!loaded && !error && <Spinner />}
      {error && <div className="inline-error">{error}</div>}
      {loaded && (
        <>
          <div className="automation-modes">
            {MODES.map((item) => (
              <button
                key={item.mode}
                type="button"
                className={`automation-mode${mode === item.mode ? " selected" : ""}`}
                aria-pressed={mode === item.mode}
                onClick={() => setMode(item.mode)}
              >
                <strong>{t(item.title)}</strong>
                <small>{t(item.body)}</small>
              </button>
            ))}
          </div>
          <label className="automation-auto-deploy">
            <input
              type="checkbox"
              checked={autoDeploy}
              onChange={(event) => setAutoDeploy(event.target.checked)}
            />
            <span>
              <strong>{t("Allow automatic deploys")}</strong>
              <small>
                {t("When this is on, changes that pass every check may go live without another click. You can turn it off at any time.")}
              </small>
            </span>
          </label>
          <div className="automation-actions">
            <button type="button" className="button primary" onClick={save} disabled={busy}>
              {busy ? <Spinner /> : null} {t("Save mode")}
            </button>
            <small className="field-hint">{t("Only the owner can change this.")}</small>
          </div>
        </>
      )}
    </section>
  );
}
