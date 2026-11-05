"use client";

import { useCallback, useEffect, useState } from "react";

import { useI18n } from "../i18n";
import type { ApiRequest } from "../types";
import { relativeTime } from "../types";
import { Spinner } from "./ui";
import { SettingRow, SettingsGroup } from "./settings";

type Circuit = {
  principal_key: string;
  reason: string;
  doc_key: string | null;
  opened_at: string;
  reopen_at: string;
};

type EmbeddingUsage = {
  mode: string;
  embedding_tokens_used: number;
  embedding_tokens_quota: number;
  writes: number;
  would_rate_limit: number;
  would_circuit_open: number;
  circuit_open: Circuit[];
};

const REASONS: Record<string, string> = {
  hot_key_loop: "Rewrote one document in a loop",
  write_spike: "Wrote far above its usual rate",
  ignored_backoff: "Kept retrying before Retry-After",
};

/** Open runaway-loop circuit breakers with one-click release (AG-ST-15). */
export function BudgetCircuits({ request }: { request: ApiRequest }) {
  const { t } = useI18n();
  const [usage, setUsage] = useState<EmbeddingUsage | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setUsage(await request<EmbeddingUsage>("/v1/usage/embedding?window=day"));
      setError(null);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    }
  }, [request]);

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(), 30_000);
    return () => window.clearInterval(timer);
  }, [load]);

  async function clear(key: string) {
    setBusy(key);
    try {
      await request(`/v1/human/budget/circuits/${encodeURIComponent(key)}/clear`, {
        method: "POST",
      });
      await load();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy(null);
    }
  }

  return (
    <SettingsGroup
      title={t("Circuit breakers")}
      description={t("Tokens paused for a runaway write loop. Release one when you have checked the agent.")}
    >
      {error && <SettingRow label={t("Could not load circuit breakers")} lamp="red" description={error} />}
      {!usage && !error && <SettingRow label={<Spinner />} />}
      {usage && (
        <>
          <SettingRow
            label={t("Embedding tokens today")}
            description={t("limit {value}", { value: usage.embedding_tokens_quota.toLocaleString() })}
          >
            <span className="setting-value tabular">{usage.embedding_tokens_used.toLocaleString()}</span>
          </SettingRow>
          <SettingRow label={t("Writes today")} description={t("mode {value}", { value: usage.mode })}>
            <span className="setting-value tabular">{usage.writes.toLocaleString()}</span>
          </SettingRow>
          <SettingRow label={t("Would have tripped")} description={t("in shadow mode")}>
            <span className="setting-value tabular">{usage.would_circuit_open}</span>
          </SettingRow>
          {usage.circuit_open.length === 0 && (
            <SettingRow lamp="green" label={t("No open circuit breakers.")} />
          )}
          {usage.circuit_open.map((circuit) => (
            <SettingRow
              key={circuit.principal_key}
              lamp="red"
              tone="need"
              label={<span className="mono">{circuit.principal_key}</span>}
              description={[
                t(REASONS[circuit.reason] ?? circuit.reason) + (circuit.doc_key ? ` · ${circuit.doc_key}` : ""),
                t("Opened {when}; reopens {until}", {
                  when: relativeTime(circuit.opened_at),
                  until: relativeTime(circuit.reopen_at),
                }),
              ].join(" · ")}
            >
              <button
                type="button"
                className="button ghost small"
                disabled={busy === circuit.principal_key}
                onClick={() => void clear(circuit.principal_key)}
              >
                {busy === circuit.principal_key ? <Spinner /> : null} {t("Release")}
              </button>
            </SettingRow>
          ))}
        </>
      )}
    </SettingsGroup>
  );
}
