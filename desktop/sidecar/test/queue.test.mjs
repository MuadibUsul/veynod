import assert from "node:assert/strict";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

process.env.VEYNOD_HOME = mkdtempSync(join(tmpdir(), "veynod-queue-"));
const { add, pending, receipt, snapshot, update } = await import("../queue.mjs");

test("a dedupe key makes a repeated assignment idempotent", () => {
    const first = add({
        runtime: "codex", cwd: "C:/work", task_key: "TASK-1",
        prompt: "go", dedupe_key: "task:zcode:TASK-1", input_id: "11111111-1111-4111-8111-111111111111",
    });
    const second = add({
        runtime: "codex", cwd: "C:/work", task_key: "TASK-1",
        prompt: "go again", dedupe_key: "task:zcode:TASK-1", input_id: "22222222-2222-4222-8222-222222222222",
    });
    assert.equal(second.input_id, first.input_id);
    assert.equal(pending().length, 1);
});

test("an item leaves pending once it is delivered and the receipt keeps its stage", () => {
    const item = add({
        runtime: "codex", cwd: "C:/work", task_key: "TASK-2",
        prompt: "go", dedupe_key: "task:zcode:TASK-2", input_id: "33333333-3333-4333-8333-333333333333",
    });
    update(item.input_id, { state: "started" });
    assert.ok(!pending().some((entry) => entry.input_id === item.input_id));
    receipt({ stage: "started", input_id: item.input_id, task_key: "TASK-2" });
    const rows = snapshot();
    assert.equal(rows.items.find((entry) => entry.input_id === item.input_id).state, "started");
    assert.equal(rows.receipts.at(-1).stage, "started");
});
