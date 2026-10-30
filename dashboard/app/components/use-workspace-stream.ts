"use client";

import { useEffect, useRef, useState } from "react";

/*
 * Follow the workspace event stream (server-sent events) and hand every event
 * to the caller, who decides what to reload. It reconnects on its own and
 * resumes from the last event id it saw; nothing here polls.
 */

export type StreamEvent = {
  id: number;
  table?: string;
  type?: string | null;
  event_type?: string;
  task_key?: string | null;
  status?: string | null;
  actor?: string | null;
  targets?: string[];
  details?: Record<string, unknown> | null;
  timestamp?: string;
};

const RETRY_MS = 4000;

function normalizeBase(value: string): string {
  return value.replace(/\/+$/, "");
}

export function useWorkspaceStream({
  apiBase,
  credential,
  path,
  onEvent,
  enabled = true,
}: {
  apiBase: string;
  credential: string;
  path: string;
  onEvent: (event: StreamEvent) => void;
  enabled?: boolean;
}): boolean {
  const [live, setLive] = useState(false);
  const onEventRef = useRef(onEvent);
  onEventRef.current = onEvent;

  useEffect(() => {
    if (!enabled) return undefined;
    let stopped = false;
    const controller = new AbortController();
    let retry: number | undefined;
    let last: number | null = null;

    async function follow() {
      if (stopped) return;
      try {
        const headers: Record<string, string> = { Authorization: `Bearer ${credential}` };
        if (last !== null) headers["Last-Event-ID"] = String(last);
        const response = await fetch(`${normalizeBase(apiBase)}${path}`, { headers, signal: controller.signal });
        if (!response.ok || !response.body) throw new Error(`SSE ${response.status}`);
        setLive(true);
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        while (!stopped) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          let split = buffer.indexOf("\n\n");
          while (split >= 0) {
            const frame = buffer.slice(0, split);
            buffer = buffer.slice(split + 2);
            split = buffer.indexOf("\n\n");
            let name = "message";
            let data = "";
            for (const line of frame.split("\n")) {
              if (line.startsWith("event:")) name = line.slice(6).trim();
              else if (line.startsWith("data:")) data += line.slice(5).trim();
            }
            if (name !== "workspace" || !data) continue;
            try {
              const event = JSON.parse(data) as StreamEvent;
              last = Math.max(last ?? 0, event.id);
              onEventRef.current(event);
            } catch {
              // a malformed frame is skipped; the next reload corrects it
            }
          }
        }
      } catch {
        // the stream dropped; try again below
      }
      if (stopped) return;
      setLive(false);
      retry = window.setTimeout(() => void follow(), RETRY_MS);
    }

    void follow();
    return () => {
      stopped = true;
      controller.abort();
      window.clearTimeout(retry);
    };
  }, [apiBase, credential, path, enabled]);

  return live;
}
