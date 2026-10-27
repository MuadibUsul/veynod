import assert from "node:assert/strict";
import test from "node:test";

import {
  CreateTaskPayloadSchema,
  HandoffBriefingSchema,
  ProtocolValidationError,
  ReviewSubmissionSchema,
  TriState,
  parseCreateTaskPayload,
  parseHandoffBriefing,
  parseReviewSubmission,
} from "../dist/index.js";

const attribution = {
  project: { mode: "VALUE", value: "Veynod" },
  repo: { mode: "VALUE", value: "MuadibUsul/AgentGraph" },
  local_path: { mode: "VALUE", value: "E:/Codes/AgentGraph" },
  deploy_target: { mode: "NA", reason: "no deploy for this task" },
};

const acceptance = [
  {
    id: "acc-one",
    kind: "MANUAL",
    statement: "The endpoint returns the documented payload for a valid request.",
  },
];

const payload = {
  task_key: "demo-task-one",
  namespace: "agentgraph",
  title: "A valid task title",
  description: "This description carries the background and the concrete goal.",
  attribution,
  acceptance,
  priority: 10,
  kind: "work",
  assigned_role: "EXECUTOR",
};

function expect422(fn) {
  let error;
  try {
    fn();
  } catch (caught) {
    error = caught;
  }
  assert.ok(error, "expected a ProtocolValidationError");
  assert.ok(error instanceof ProtocolValidationError, `unexpected error: ${error}`);
  assert.equal(error.status, 422);
  assert.equal(error.code, "validation_error");
  return error;
}

test("exports the four required schemas", () => {
  assert.equal(typeof TriState, "function");
  assert.ok(CreateTaskPayloadSchema && HandoffBriefingSchema && ReviewSubmissionSchema);
});

test("accepts a complete create-task payload", () => {
  const parsed = parseCreateTaskPayload(payload);
  assert.equal(parsed.task_key, "demo-task-one");
  assert.equal(parsed.max_retries, 3);
  assert.equal(parsed.requires_code_delivery, false);
  assert.equal(parsed.assignee, null);
});

test("rejects an empty acceptance array with 422", () => {
  const error = expect422(() => parseCreateTaskPayload({ ...payload, acceptance: [] }));
  assert.ok(error.issues.some((issue) => issue.path.includes("acceptance")));
});

test("rejects a vague acceptance statement with 422", () => {
  expect422(() =>
    parseCreateTaskPayload({
      ...payload,
      acceptance: [{ id: "acc-vague", kind: "MANUAL", statement: "优化一下" }],
    }),
  );
});

test("rejects an AUTO acceptance item without an auto_runner_id", () => {
  expect422(() =>
    parseCreateTaskPayload({
      ...payload,
      acceptance: [
        {
          id: "acc-auto",
          kind: "AUTO",
          statement: "The migration applies cleanly on an empty database.",
        },
      ],
    }),
  );
});

test("rejects a namespace-prefixed context doc URI with 422", () => {
  expect422(() =>
    parseCreateTaskPayload({
      ...payload,
      context_docs: [{ uri: "doc://agentgraph/veynod-role-rules", version: 2 }],
    }),
  );
});

test("accepts the short context doc URI with a pinned version", () => {
  const parsed = parseCreateTaskPayload({
    ...payload,
    context_docs: [{ uri: "doc://veynod-role-rules", version: 2 }],
  });
  assert.deepEqual(parsed.context_docs, [{ uri: "doc://veynod-role-rules", version: 2 }]);
});

test("accepts PENDING and NA tri-state values", () => {
  const parsed = parseCreateTaskPayload({
    ...payload,
    attribution: {
      project: { mode: "VALUE", value: "Veynod" },
      repo: { mode: "VALUE", value: "MuadibUsul/AgentGraph" },
      local_path: {
        mode: "PENDING",
        field_name: "local_path",
        escalate_to: "agent:commander",
        reason: "the claimer fills in the local checkout path",
      },
      deploy_target: { mode: "NA", reason: "pure platform task" },
    },
  });
  assert.equal(parsed.attribution.local_path.mode, "PENDING");
});

test("rejects a handoff briefing without a risk_flag and a bad code_delivery url", () => {
  expect422(() =>
    parseHandoffBriefing({
      fencing_token: 1,
      output_location: "doc://handoff/task-one",
      summary: "Delivered the schema package and the migration.",
      key_metrics: { tests: 12 },
      unverified_hypotheses: [],
    }),
  );
  expect422(() =>
    parseHandoffBriefing({
      fencing_token: 1,
      output_location: "doc://handoff/task-one",
      code_delivery: { branch_url: "not-a-url", commit: "abc1234" },
      summary: "Delivered the schema package and the migration.",
      key_metrics: { tests: 12 },
      unverified_hypotheses: [],
      risk_flag: "LOW",
    }),
  );
});

test("accepts a handoff briefing with code_delivery", () => {
  const parsed = parseHandoffBriefing({
    fencing_token: 1,
    output_location: "https://github.com/MuadibUsul/veynod/pull/1",
    code_delivery: {
      branch_url: "https://github.com/MuadibUsul/veynod/tree/feat/protocol",
      pr_url: "https://github.com/MuadibUsul/veynod/pull/1",
      commit: "9dbac7f",
    },
    summary: "Delivered the schema package and the migration.",
    key_metrics: { tests: 12, risk: "low" },
    unverified_hypotheses: [],
    risk_flag: "LOW",
  });
  assert.equal(parsed.code_delivery.commit, "9dbac7f");
});

test("rejects a FAIL verdict without rework guidance", () => {
  expect422(() =>
    parseReviewSubmission({
      verdicts: [{ acceptance_id: "acc-one", result: "FAIL" }],
    }),
  );
});

test("accepts per-item PASS and FAIL verdicts", () => {
  const parsed = parseReviewSubmission({
    verdicts: [
      { acceptance_id: "acc-one", result: "PASS", evidence_doc: { uri: "doc://evidence", version: 1 } },
      {
        acceptance_id: "acc-two",
        result: "FAIL",
        rework_target: "the migration script",
        rework_standard: "it must apply on an empty database",
        reverify_by: "AUDITOR",
      },
    ],
  });
  assert.equal(parsed.verdicts.length, 2);
  assert.equal(parsed.verdicts[0].result, "PASS");
  const failed = parsed.verdicts[1];
  assert.equal(failed.result, "FAIL");
  assert.equal(failed.reverify_by, "AUDITOR");
});
