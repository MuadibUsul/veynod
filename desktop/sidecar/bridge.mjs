#!/usr/bin/env node
// JSON-lines bridge used by the Tauri client. Methods stay small and explicit;
// the client never imports OAR in the webview.
import { createInterface } from "node:readline";
import { describe } from "./describe.mjs";
import { inventory } from "./inventory.mjs";

const methods = {
  inventory,
  describe: (params) => describe(params.runtime),
};

async function handle(line) {
  let request;
  try {
    request = JSON.parse(line);
  } catch {
    return { id: null, ok: false, error: "invalid json" };
  }
  const handler = methods[request.method];
  if (!handler) return { id: request.id ?? null, ok: false, error: `unknown method ${request.method}` };
  try {
    return { id: request.id ?? null, ok: true, result: await handler(request.params ?? {}) };
  } catch (error) {
    return { id: request.id ?? null, ok: false, error: error instanceof Error ? error.message : String(error) };
  }
}

if (process.argv.includes("--inventory")) {
  console.log(JSON.stringify(await inventory(), null, 2));
} else if (process.argv.includes("--describe")) {
  const index = process.argv.indexOf("--describe");
  console.log(JSON.stringify(await describe(process.argv[index + 1]), null, 2));
} else {
  const rl = createInterface({ input: process.stdin, crlfDelay: Infinity });
  for await (const line of rl) {
    if (!line.trim()) continue;
    console.log(JSON.stringify(await handle(line)));
  }
}
