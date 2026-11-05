"use client";

import { useCallback, useEffect, useState } from "react";

import { useI18n } from "../i18n";
import { normalizeBase, relativeTime } from "../types";
import { SettingRow, SettingsFooter, SettingsGroup, Switch } from "./settings";

type Grant = {
  id: string;
  note: string;
  allow_write: boolean;
  created_at: string;
  expires_at: string;
  revoked_at: string | null;
  requests: number;
  last_request_at: string | null;
};

type SupportStatus = {
  private: boolean;
  operator_managed: boolean;
  active: Grant | null;
  grants: Grant[];
  operator_requests: { at: string; method: string; path: string }[];
};

// Owner-side privacy card: the operator sees counts and changes, never
// content, unless the owner opens a time-limited support window here.
export function SupportAccessCard({ apiBase, sessionToken }: { apiBase: string; sessionToken: string }) {
  const { t } = useI18n();
  const [state, setState] = useState<SupportStatus | null>(null);
  const [hours, setHours] = useState(24);
  const [note, setNote] = useState("");
  const [allowWrite, setAllowWrite] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const call = useCallback(
    async (path: string, init: RequestInit = {}) => {
      const response = await fetch(`${normalizeBase(apiBase)}/v1/human/support-access${path}`, {
        ...init,
        headers: { Authorization: `Bearer ${sessionToken}`, "Content-Type": "application/json" },
        signal: AbortSignal.timeout(15000),
      });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(body?.error?.message ?? `HTTP ${response.status}`);
      return body as SupportStatus;
    },
    [apiBase, sessionToken],
  );

  const load = useCallback(async () => {
    try {
      setState(await call(""));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    }
  }, [call]);

  useEffect(() => {
    void load();
  }, [load]);

  async function act(path: string, init: RequestInit) {
    setBusy(true);
    setError(null);
    try {
      setState(await call(path, init));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy(false);
    }
  }

  if (!state) return null;
  const active = state.active;

  return (
    <SettingsGroup
      title={t("Privacy and support access")}
      description={t("Only when you ask Veynod for help: open a window, and close it when you are done. Every request the operator makes is listed below.")}
    >
      {error && <SettingRow label={t("Could not change support access")} lamp="red" description={error} />}
      <SettingRow
        lamp={state.private ? "green" : "amber"}
        label={state.private ? t("Private") : t("Operator-managed")}
        description={state.private
          ? t("This workspace is private. The Veynod operator sees its counts and changes, never its documents, tasks or audit.")
          : t("The Veynod operator created this workspace and can read it. Make it private to keep the operator to counts and changes.")}
      >
        {!state.private && (
          <button
            type="button"
            className="button primary small"
            disabled={busy}
            onClick={() => void act("/make-private", { method: "POST", body: "{}" })}
          >
            {t("Make this workspace private")}
          </button>
        )}
      </SettingRow>

      {state.private && active && (
        <SettingRow
          lamp="cyan"
          label={active.allow_write
            ? t("Support can read and change this workspace until {time}.", {
                time: new Date(active.expires_at).toLocaleString(),
              })
            : t("Support can read this workspace until {time}.", {
                time: new Date(active.expires_at).toLocaleString(),
              })}
          description={`${t("{count} operator requests so far.", { count: active.requests })}${active.note ? ` · ${active.note}` : ""}`}
        >
          <button
            type="button"
            className="button ghost small"
            disabled={busy}
            onClick={() => void act("", { method: "DELETE" })}
          >
            {t("Close support access now")}
          </button>
        </SettingRow>
      )}

      {state.private && !active && (
        <form
          className="setting-form flush"
          onSubmit={(event) => {
            event.preventDefault();
            void act("", {
              method: "POST",
              body: JSON.stringify({ hours, note, allow_write: allowWrite }),
            });
          }}
        >
          <SettingRow htmlFor="support-hours" label={t("For how long")}>
            <select id="support-hours" value={hours} onChange={(event) => setHours(Number(event.target.value))}>
              <option value={1}>{t("1 hour")}</option>
              <option value={24}>{t("24 hours")}</option>
              <option value={72}>{t("3 days")}</option>
              <option value={168}>{t("7 days")}</option>
            </select>
          </SettingRow>
          <SettingRow htmlFor="support-note" label={t("Why (optional)")}>
            <input id="support-note" value={note} onChange={(event) => setNote(event.target.value)} maxLength={500} />
          </SettingRow>
          <SettingRow label={t("Also allow changes (default: read-only)")}>
            <Switch checked={allowWrite} onChange={setAllowWrite} label={t("Also allow changes (default: read-only)")} />
          </SettingRow>
          <SettingsFooter>
            <button type="submit" className="button ghost small" disabled={busy}>
              {t("Open support access")}
            </button>
          </SettingsFooter>
        </form>
      )}

      {state.operator_requests.length > 0 && (
        <SettingRow
          stack
          label={t("Operator requests ({count})", { count: state.operator_requests.length })}
        >
          <details className="setting-log">
            <summary>{t("Show")}</summary>
            <ul>
              {state.operator_requests.map((item, index) => (
                <li key={`${item.at}-${index}`}>
                  <span className="muted">{relativeTime(item.at)}</span>
                  <code>{item.method} {item.path}</code>
                </li>
              ))}
            </ul>
          </details>
        </SettingRow>
      )}
    </SettingsGroup>
  );
}
