"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { useI18n } from "../i18n";
import type { ApiRequest } from "../types";
import { IconCheck, IconCopy, Modal, Spinner } from "./ui";

/*
 * Sign a computer's agent in to the owner's own subscription, from here (v2
 * phase 1, P1-D). For a server: nobody sits at it and it has no browser. The
 * computer runs the agent's own sign-in and sends back only a link on the
 * provider's site (and for Codex a one-time code); the person signs in in
 * their own browser. The login stays on the computer. The server relays
 * (services/logins.py); the computer's side is cli/bin/login.mjs.
 */

type Runtime = "claude" | "codex";

type Login = {
  login_id: string;
  runtime: Runtime;
  runtime_name: string;
  subscription: string;
  state: "starting" | "enter_code" | "open_url" | "verifying" | "connected" | "failed" | "cancelled";
  url: string | null;
  user_code: string | null;
  expires_at: string | null;
  plan: string | null;
  message: string | null;
};

const CHOICES: { runtime: Runtime; name: string; plan: string }[] = [
  { runtime: "claude", name: "Claude Code", plan: "Claude Pro or Max" },
  { runtime: "codex", name: "Codex", plan: "ChatGPT Plus or Pro" },
];

const DONE = new Set<Login["state"]>(["connected", "failed", "cancelled"]);
const POLL_MS = 1500;

export function SubscriptionConnect({
  request,
  hostId,
  computerName,
  logins,
  signedIn,
  onClose,
  onConnected,
}: {
  request: ApiRequest;
  /** The computer's work seat, which runs its agents. */
  hostId: string;
  computerName: string;
  /** What this computer can sign in from here. */
  logins: string[];
  /** What it already signed in through Veynod. */
  signedIn: string[];
  onClose: () => void;
  onConnected: () => void;
}) {
  const { t } = useI18n();
  const [login, setLogin] = useState<Login | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [code, setCode] = useState("");
  const [sending, setSending] = useState(false);
  const [copied, setCopied] = useState(false);
  const loginRef = useRef<Login | null>(null);
  loginRef.current = login;
  const base = `/v1/runner/hosts/${encodeURIComponent(hostId)}/logins`;

  const start = useCallback(async (runtime: Runtime) => {
    setError(null);
    setCode("");
    try {
      setLogin(await request<Login>(base, { method: "POST", body: JSON.stringify({ runtime }) }));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, [base, request]);

  // Follow the sign-in until it ends; the computer moves it, not this page.
  useEffect(() => {
    if (!login || DONE.has(login.state)) return;
    const timer = window.setTimeout(async () => {
      try {
        const next = await request<Login>(`${base}/${encodeURIComponent(login.login_id)}`);
        setLogin(next);
        if (next.state === "connected") onConnected();
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err));
      }
    }, POLL_MS);
    return () => window.clearTimeout(timer);
  }, [base, login, onConnected, request]);

  // Closing before the end stops the sign-in on the computer too.
  const close = useCallback(() => {
    const open = loginRef.current;
    if (open && !DONE.has(open.state)) {
      void request(`${base}/${encodeURIComponent(open.login_id)}/cancel`, { method: "POST" }).catch(() => {});
    }
    onClose();
  }, [base, onClose, request]);

  async function sendCode() {
    if (!login) return;
    setSending(true);
    setError(null);
    try {
      setLogin(await request<Login>(`${base}/${encodeURIComponent(login.login_id)}/code`, {
        method: "POST", body: JSON.stringify({ code: code.trim() }),
      }));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSending(false);
    }
  }

  async function copy(text: string) {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      // the code stays on screen to type
    }
  }

  const choices = CHOICES.filter((choice) => logins.includes(choice.runtime));
  const minutesLeft = login?.expires_at
    ? Math.max(1, Math.round((Date.parse(login.expires_at) - Date.now()) / 60_000)) : null;

  let body;
  let footer;
  if (!login) {
    body = (
      <div className="connect-sub">
        <p>{t("Choose the subscription {name} should work on. You sign in in your own browser; the login stays on {name} and Veynod never sees it.", { name: computerName })}</p>
        <div className="connect-sub-choices">
          {choices.map((choice) => (
            <button key={choice.runtime} type="button" className="connect-sub-choice" onClick={() => void start(choice.runtime)}>
              <b>{choice.name}</b>
              <span>{t(choice.plan)}</span>
              {signedIn.includes(choice.runtime) && <small>{t("Signed in through Veynod: sign in again")}</small>}
            </button>
          ))}
        </div>
        <p className="connect-sub-terms">
          {t("Your subscription is used under its provider's terms, for this workspace's work only.")}
        </p>
      </div>
    );
  } else if (login.state === "starting") {
    body = <p className="connect-sub-wait"><Spinner /> {t("Asking {name} to start the sign-in…", { name: computerName })}</p>;
  } else if (login.state === "enter_code" && login.url) {
    body = (
      <ol className="connect-sub-steps">
        <li>
          <a className="button primary small" href={login.url} target="_blank" rel="noopener noreferrer">
            {t("Open the Claude sign-in page")}
          </a>
        </li>
        <li>{t("Sign in with the Claude account whose Pro or Max plan {name} should use.", { name: computerName })}</li>
        <li>
          <label htmlFor="connect-sub-code">{t("Copy the code the page shows, then paste it here:")}</label>
          <div className="connect-sub-code">
            <input id="connect-sub-code" value={code} autoComplete="off" spellCheck={false}
              onChange={(event) => setCode(event.target.value)}
              onKeyDown={(event) => { if (event.key === "Enter" && code.trim()) void sendCode(); }} />
            <button type="button" className="button primary small" disabled={!code.trim() || sending}
              onClick={() => void sendCode()}>{t("Connect")}</button>
          </div>
        </li>
      </ol>
    );
  } else if (login.state === "open_url" && login.url && login.user_code) {
    body = (
      <ol className="connect-sub-steps">
        <li>
          <a className="button primary small" href={login.url} target="_blank" rel="noopener noreferrer">
            {t("Open {site}", { site: new URL(login.url).host })}
          </a>
        </li>
        <li>{t("Sign in with the ChatGPT account whose Plus or Pro plan {name} should use.", { name: computerName })}</li>
        <li>
          {t("Enter this code there:")}
          <span className="connect-sub-device">
            <code>{login.user_code}</code>
            <button type="button" className="icon-button" aria-label={t("Copy the code")}
              onClick={() => void copy(login.user_code!)}>
              {copied ? <IconCheck /> : <IconCopy />}
            </button>
          </span>
          <small>
            {minutesLeft !== null ? `${t("It expires in {n} minutes.", { n: minutesLeft })} ` : ""}
            {t("Enter it only because you started this sign-in just now.")}
          </small>
        </li>
        <li className="connect-sub-wait"><Spinner /> {t("Waiting for you to sign in…")}</li>
      </ol>
    );
  } else if (login.state === "verifying") {
    body = <p className="connect-sub-wait"><Spinner /> {t("Checking the subscription on {name}…", { name: computerName })}</p>;
  } else if (login.state === "connected") {
    body = (
      <p className="connect-sub-done">
        <IconCheck />{" "}
        {login.plan
          ? t("{name} now runs {runtime} on your {plan} plan.", { name: computerName, runtime: login.runtime_name, plan: login.plan })
          : t("{name} now runs {runtime} on your subscription.", { name: computerName, runtime: login.runtime_name })}
      </p>
    );
    footer = <button type="button" className="button primary" onClick={onClose}>{t("Done")}</button>;
  } else {
    body = (
      <p className="connect-sub-failed">
        {login.state === "cancelled" ? t("The sign-in was stopped.")
          : login.message ? t(login.message) : t("The sign-in did not finish.")}
      </p>
    );
    footer = (
      <button type="button" className="button primary" onClick={() => void start(login.runtime)}>
        {t("Start again")}
      </button>
    );
  }

  return (
    <Modal title={t("Connect a subscription")} subtitle={computerName} onClose={close} footer={footer}>
      {body}
      {error && <p className="connect-sub-failed" role="alert">{error}</p>}
    </Modal>
  );
}
