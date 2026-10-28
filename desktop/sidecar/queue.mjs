import { appendFileSync, existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { homedir } from "node:os";

const dir = process.env.VEYNOD_HOME || join(homedir(), ".veynod");
const queueFile = join(dir, "wake-queue.json");
const receiptFile = join(dir, "wake-receipts.jsonl");

function ensure() { mkdirSync(dir, { recursive: true }); }
function load() {
  try { return JSON.parse(readFileSync(queueFile, "utf8")); } catch { return []; }
}
function save(items) { ensure(); writeFileSync(queueFile, JSON.stringify(items, null, 2)); }

export function add(item) {
  const items = load();
  const dedupe = item.dedupe_key || item.input_id;
  if (dedupe && items.some((entry) => (entry.dedupe_key || entry.input_id) === dedupe)) {
    return items.find((entry) => (entry.dedupe_key || entry.input_id) === dedupe);
  }
  const entry = { state: "pending", attempts: 0, created_at: new Date().toISOString(), ...item };
  items.push(entry);
  save(items);
  return entry;
}
export function pending() { return load().filter((entry) => entry.state === "pending"); }
export function update(inputId, patch) {
  const items = load();
  const index = items.findIndex((entry) => entry.input_id === inputId);
  if (index >= 0) { items[index] = { ...items[index], ...patch }; save(items); }
  return items[index];
}
export function snapshot() {
  const receipts = [];
  if (existsSync(receiptFile)) {
    for (const line of readFileSync(receiptFile, "utf8").trim().split("\n").slice(-40)) {
      try { receipts.push(JSON.parse(line)); } catch { /* ignore a torn line */ }
    }
  }
  return { items: load(), receipts };
}
export function receipt(value) {
  ensure();
  appendFileSync(receiptFile, JSON.stringify({ at: new Date().toISOString(), ...value }) + "\n");
}
