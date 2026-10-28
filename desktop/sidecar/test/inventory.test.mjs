import assert from "node:assert/strict";
import test from "node:test";
import { inventory } from "../inventory.mjs";

test("inventory reports each supported runtime without throwing", async () => {
  const items = await inventory();
  assert.ok(Array.isArray(items) && items.length >= 2);
  for (const item of items) {
    assert.equal(typeof item.id, "string");
    assert.equal(typeof item.available, "boolean");
  }
  assert.ok(items.some((item) => item.id === "claude"));
  assert.ok(items.some((item) => item.id === "codex"));
});
